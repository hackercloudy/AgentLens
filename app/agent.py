"""The agent loop: a small ReAct-style controller (think -> call tool -> observe -> answer).

It is a generator that yields trace events, so the UI can stream every step live and the same code
serves the API, the eval runner and the tests.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timezone

from . import config, grounding, safety, tools
from .ingest import KnowledgeBase
from .llm import LLMError, get_llm, iter_chat
from .prompts import GENERAL_SYSTEM, GROUNDED_SYSTEM, OBS_PREFIX, build_system
from .tokens import count_tokens

ABSTAIN_RE = re.compile(
    r"don'?t know|do not know|no information|not mention|couldn'?t find|can'?t find|cannot find|unable to|"
    r"can'?t share|can'?t help|not available|insufficient|no relevant|not able to|no data|no matching",
    re.I,
)


@dataclass
class AgentConfig:
    model: str = "mock:offline-demo"
    temperature: float = 0.2
    top_p: float = 0.95
    max_tokens: int = 900
    max_steps: int = 5
    use_docs: bool = True          # turning this off = "RAG off"
    use_sql: bool = True
    use_calc: bool = True
    use_web: bool = True           # web_search tool (needs internet)
    router: bool = True            # fast path: route the question, search first, ONE model call (real models only)
    top_k: int = 4
    retrieval: str = "hybrid"      # dense | bm25 | hybrid
    min_score: float = 0.12
    chunk_size: int | None = None  # None = knowledge-base default
    overlap: int | None = None
    embedder: str | None = None
    prompt_style: str = "zero-shot"  # zero-shot | few-shot | cot
    language: str = "auto"
    guardrails: bool = True
    general_knowledge: bool = True  # allow answers from the model's own knowledge when tools are not enough
    context_window: int = config.DEFAULT_CONTEXT_WINDOW
    extra_pages: list = field(default_factory=list)  # (doc, page, text) tuples, used by the Red Team runner

    @classmethod
    def from_dict(cls, d: dict | None) -> "AgentConfig":
        d = d or {}
        allowed = {f.name for f in fields(cls)}
        cfg = cls(**{k: v for k, v in d.items() if k in allowed and v is not None})
        cfg.temperature = min(2.0, max(0.0, float(cfg.temperature)))
        cfg.top_p = min(1.0, max(0.05, float(cfg.top_p)))
        cfg.top_k = min(10, max(1, int(cfg.top_k)))
        cfg.max_steps = min(8, max(1, int(cfg.max_steps)))
        cfg.max_tokens = min(2048, max(64, int(cfg.max_tokens)))
        cfg.context_window = min(131072, max(1024, int(cfg.context_window)))
        if cfg.retrieval not in ("dense", "bm25", "hybrid"):
            cfg.retrieval = "hybrid"
        if cfg.prompt_style not in ("zero-shot", "few-shot", "cot"):
            cfg.prompt_style = "zero-shot"
        return cfg

    def public(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "extra_pages"}
        return d


def parse_action(text: str) -> dict | None:
    """Extract the JSON action from a model reply, tolerating code fences and chatter."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    for candidate in (t, t[t.find("{") : t.rfind("}") + 1] if "{" in t else ""):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "action" in obj:
            return obj
    return None


_INPUT_KEYS = ("answer", "final_answer", "text", "content", "response", "result", "message", "sql", "query", "expression", "code", "q")


def normalise_input(arg, final: bool = False) -> str:
    """Small models often wrap the answer in an object ({"answer": "..."}) or double-escape newlines. Undo that."""
    if isinstance(arg, dict):
        for k in _INPUT_KEYS:
            if isinstance(arg.get(k), str):
                arg = arg[k]
                break
        else:
            arg = "\n".join(str(v) for v in arg.values())
    elif isinstance(arg, list):
        arg = "\n".join(str(x) for x in arg)
    elif not isinstance(arg, str):
        arg = str(arg)
    text = arg.strip()
    if final and text.startswith("{") and text.endswith("}"):
        try:
            return normalise_input(json.loads(text), final=True)
        except json.JSONDecodeError:
            pass
    if final and "\\n" in arg and "\n" not in arg:
        arg = arg.replace("\\n", "\n").replace("\\t", "\t")
    return arg


# Questions that are about the outside world, where a small model's memory is unreliable.
_FACTUAL_RE = re.compile(
    r"\b(who|whom|whose|when|where|latest|current|currently|recent|recently|news|today|tonight|yesterday|this (?:week|month|year)|"
    r"price|cost of|born|birthday|age of|how old|died|death|won|winner|champion|ceo|founder|president|prime minister|minister|capital of|"
    r"population|how many|how much|score|result|released?|launch(?:ed)?|son|daughter|wife|husband|father|mother|brother|sister|"
    r"family|net worth|name of|full name|nationality)\b", re.I)
_CREATIVE_RE = re.compile(r"^\s*(write|explain|code|create|generate|translate|summari[sz]e|rewrite|fix|debug|implement|draw|compose|brainstorm|give me (?:a|an|some))\b", re.I)
_LOCAL_RE = re.compile(r"\b(zentrovax|cardiolin|respira|gastrel|neurazen|dermapure|dosage|dose|sales|revenue|orders?|accounts?|region|policy|handbook|sop|discount|storage|breach|document|database)\b", re.I)
_DATA_RE = re.compile(r"\b(sales?|revenue|orders?|accounts?|regions?|units?|quarter|customers?|discount|products?|sold|database|sql|table|zentrovax|cardiolin|respira|gastrel|neurazen|dermapure|email|contact)\b", re.I)


def needs_web(question: str) -> bool:
    """Search-first policy: factual questions about the world should not be answered from a small model's memory."""
    q = question.strip()
    return bool(_FACTUAL_RE.search(q)) and not _CREATIVE_RE.match(q) and not _LOCAL_RE.search(q)


_ARITH_RE = re.compile(r"^\s*(?:what\s+is|what's|calculate|compute|solve)?\s*([\d\s.+\-*/()^%,x\u00d7]+?)\s*\??\s*$", re.I)
_CONCEPT_RE = re.compile(r"^\s*(?:explain|define|describe|compare|how does|how do|how to|why does|why do|why is|what (?:is|are) (?:a|an)\b|what does .* mean)|\bdifference between\b|\bpros and cons\b", re.I)
_CHAT_RE = re.compile(r"^\s*(hi|hello|hey|thanks|thank you|ok|okay|good (?:morning|afternoon|evening)|who are you|what can you do)\b", re.I)
_DOC_RE = re.compile(r"\b(zentrovax|dosage|dose|dosing|policy|handbook|sop|storage|breach|cold chain|consent|adverse|retention|documents?|guideline|excursion|renal|egfr|missed dose|discount approval)\b", re.I)
_SQL_RE = re.compile(r"\b(sales?|revenue|orders?|accounts?|regions?|units?|quarter|customers?|sold|emails?|contacts?|average discount|best[- ]selling|sales database|our database)\b", re.I)
_FUN_RE = re.compile(r"\b(joke|poem|story|riddle|haiku|essay|limerick|song lyrics)\b", re.I)
_FOLLOWUP_RE = re.compile(r"^(?:and |what about |how about )?(?:his|her|their|its|he|she|they|him|them|that|this|those|these)\b|\b(another|other|also|else)\b", re.I)


def choose_route(question: str, cfg: "AgentConfig", enabled: list[str]) -> tuple[str, str]:
    """Deterministic router (no model call). Returns (route, reason). Route 'agent' = the slower tool-choosing loop."""
    q = question.strip()
    if not cfg.router or not cfg.general_knowledge or cfg.model.startswith("mock:"):
        return "agent", "agent loop"
    if _ARITH_RE.match(q) and re.search(r"[+\-*/^%x\u00d7]", q) and "calculator" in enabled:
        return "calc", "Arithmetic: using the calculator, no model needed."
    doc, sql = bool(_DOC_RE.search(q)), bool(_SQL_RE.search(q))
    if sql and cfg.use_sql:
        return "agent", "Needs the sales database: using the tool-calling agent."
    if doc and cfg.use_docs:
        return "docs", "About your documents: searching them first."
    if _CHAT_RE.match(q):
        return "general", "Conversation: answering directly."
    factual = bool(_FACTUAL_RE.search(q))
    if (_CREATIVE_RE.match(q) or _CONCEPT_RE.match(q) or _FUN_RE.search(q)) and not factual:
        return "general", "Explanation or writing task: answering directly (fastest)."
    if "web_search" in enabled:
        return "web", "Factual question: searching the web first, then answering only from the results."
    return "general", "Answering from model knowledge."


def build_query(question: str, history: list[dict]) -> str:
    q = re.sub(r"\s+", " ", question.strip().rstrip("?")).strip()
    prev = next((m["content"] for m in reversed(history or []) if m.get("role") == "user"), "")
    if prev and (_FOLLOWUP_RE.search(q) or len(q.split()) <= 3):
        q = f"{prev.strip().rstrip('?')} {q}"
    if re.search(r"\b(latest|current|currently|recent|recently|today|news|this week|this month)\b", q, re.I) and not re.search(r"\b20\d\d\b", q):
        q += f" {date.today().year}"
    return q[:220]


def _budget(messages: list[dict], window: int) -> dict:
    sys_t = count_tokens(messages[0]["content"])
    obs = hist = q = steps = 0
    last_q = max((i for i, m in enumerate(messages) if m["role"] == "user" and not m["content"].startswith(OBS_PREFIX)), default=1)
    for i, m in enumerate(messages[1:], start=1):
        n = count_tokens(m["content"])
        if m["role"] == "user" and m["content"].startswith(OBS_PREFIX):
            obs += n
        elif i == last_q:
            q += n
        elif i < last_q:
            hist += n
        else:
            steps += n
    used = sys_t + obs + hist + q + steps
    return {
        "window": window,
        "used": used,
        "system": sys_t,
        "history": hist,
        "question": q,
        "retrieved": obs,
        "steps": steps,
        "percent": round(100 * used / window, 1),
    }


def _log(entry: dict) -> None:
    try:
        with open(config.LOG_DIR / "queries.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def run_agent(question: str, history: list[dict] | None, cfg: AgentConfig, kb: KnowledgeBase):
    """Generator of events: {"type": "step", ...} ... then one {"type": "final", ...}."""
    t_start = time.time()
    history = [m for m in (history or []) if m.get("role") in ("user", "assistant") and m.get("content")][-6:]
    steps_log: list[dict] = []
    guard_events: list[dict] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0, "llm_ms": 0, "estimated": False}
    registry = tools.SourceRegistry()
    tools_used: list[str] = []
    retrievals: list[dict] = []

    def step(kind: str, title: str, text: str = "", **extra) -> dict:
        s = {"type": "step", "i": len(steps_log) + 1, "kind": kind, "title": title, "text": text, "t": int((time.time() - t_start) * 1000), **extra}
        steps_log.append(s)
        return s

    def finish(answer: str, blocked: bool = False, messages: list[dict] | None = None, error: str | None = None, extra: dict | None = None) -> dict:
        used_ids = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
        sources = [
            {"n": i + 1, **{k: v for k, v in item.items() if k not in ("text_for_model",)}, "cited": (i + 1) in used_ids}
            for i, item in enumerate(registry.items)
        ]
        result = {
            "type": "final",
            "answer": answer,
            "blocked": blocked,
            "error": error,
            "abstained": bool(ABSTAIN_RE.search(answer)) and not blocked,
            "sources": sources,
            "tools_used": tools_used,
            "guard_events": guard_events,
            "retrievals": retrievals,
            "usage": {**usage, "total_tokens": usage["prompt_tokens"] + usage["completion_tokens"]},
            "budget": _budget(messages, cfg.context_window) if messages else None,
            "latency_ms": int((time.time() - t_start) * 1000),
            "model": cfg.model,
            "config": cfg.public(),
            "steps": len(steps_log),
        }
        result.update(extra or {})
        masked_q, _ = safety.mask_pii(question)
        _log({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "question": masked_q, "model": cfg.model, "tools": tools_used, "blocked": blocked,
            "abstained": result["abstained"], "latency_ms": result["latency_ms"], "tokens": result["usage"]["total_tokens"],
        })
        return result

    # ------------------------------------------------------------------ 1. input guard
    if cfg.guardrails:
        allowed, findings = safety.check_input(question)
        if findings:
            ev = {"guard": "input-guard", "findings": [f.to_dict() for f in findings], "blocked": not allowed}
            guard_events.append(ev)
            yield step("guard", "Input guard", ("Blocked: " if not allowed else "Flagged: ") + ", ".join(sorted({f.rule for f in findings})), data=ev)
        if not allowed:
            yield finish("I can't follow that request: it looks like an attempt to override my instructions or extract confidential data. I'm happy to help with questions about the documents or the sales data instead.", blocked=True)
            return

    # ------------------------------------------------------------------ 2. set-up
    enabled = (["search_docs"] if cfg.use_docs else []) + (["run_sql"] if cfg.use_sql else []) + (["calculator"] if cfg.use_calc else []) + (["web_search"] if cfg.use_web else [])
    if cfg.general_knowledge and cfg.use_sql and not _DATA_RE.search(question) and not cfg.model.startswith("mock:"):
        enabled = [t for t in enabled if t != "run_sql"]  # no database words in the question: keep the prompt short
    schema = tools.get_schema() if "run_sql" in enabled else ""
    system = build_system(enabled, schema, cfg.language, cfg.prompt_style, cfg.guardrails, cfg.general_knowledge)
    messages: list[dict] = [{"role": "system", "content": system}, *history, {"role": "user", "content": question}]
    store = None
    if cfg.use_docs:
        try:
            store = kb.store(cfg.chunk_size, cfg.overlap, cfg.embedder, extra_pages=cfg.extra_pages or None)
        except Exception as exc:  # noqa: BLE001
            yield step("error", "Index error", str(exc))
            yield finish(f"I couldn't open the knowledge base: {exc}", error=str(exc), messages=messages)
            return
    try:
        llm = get_llm(cfg.model)
    except LLMError as exc:
        yield step("error", "Model error", str(exc))
        yield finish(str(exc), error=str(exc), messages=messages)
        return

    yield step("info", "Plan", f"Model {cfg.model} · tools: {', '.join(enabled) or 'none'} · prompt: {cfg.prompt_style}", data={"tools": enabled})


    # ------------------------------------------------------------------ 2b. fast routed pipeline (real models)
    def tool_events(name: str, arg: str):
        yield step("tool", f"Call {name}", arg, tool=name, input=arg)
        t_tool = time.time()
        if name == "search_docs":
            out = tools.search_docs(store, arg, registry, k=cfg.top_k, mode=cfg.retrieval, min_score=cfg.min_score, sanitize=cfg.guardrails)
            retrievals.append(out.data)
        elif name == "web_search":
            out = tools.web_search(arg, registry, k=5, sanitize=cfg.guardrails)
        else:
            out = tools.calculator(arg)
        tools_used.append(name)
        guard_events.extend(out.guard_events)
        yield step("observation", "Observation", out.text, tool=name, ok=out.ok, data=out.data, ms=int((time.time() - t_tool) * 1000), guards=out.guard_events)
        for ev in out.guard_events:
            if ev.get("guard") == "context-sanitiser":
                yield step("guard", "Context sanitiser", "Removed instruction-like text from a result before the model saw it.", data=ev)
        return out

    def routed(route: str, reason: str):
        yield step("info", "Route", reason, data={"route": route})
        evidence, query = "", ""
        if route == "calc":
            m = _ARITH_RE.match(question.strip())
            out = yield from tool_events("calculator", m.group(1).strip() if m else question)
            text = out.text if out.ok else "I couldn't evaluate that expression."
            yield step("answer", "Answer", text)
            yield finish(text, messages=None)
            return
        if route in ("docs", "web"):
            query = build_query(question, history)
            if route == "docs":
                out = yield from tool_events("search_docs", query)
                if out.text.startswith("No relevant"):
                    if "web_search" in enabled:
                        route = "web"
                        yield step("info", "Route", "Nothing relevant in your documents, so trying the web.", data={"route": "web"})
                else:
                    evidence = out.text
            if route == "web":
                out = yield from tool_events("web_search", query)
                evidence = out.text if out.ok else ""
        lang = f"Write the answer in {cfg.language}." if cfg.language != "auto" else "Write the answer in the same language as the question."
        if evidence:
            system = GROUNDED_SYSTEM.format(lang=lang, today=date.today().isoformat())
            user_msg = f"Question: {question}\n\nResults:\n{evidence}"
            context, temp, max_tok = history[-2:], min(cfg.temperature, 0.1), min(cfg.max_tokens, 400)
            how = "Writing the answer using only the results above."
        else:
            note = ""
            if route in ("web", "docs"):
                note = ' You could not retrieve any evidence: start with "I could not check the web, so this may be out of date.", then answer only if confident, otherwise say you do not know.'
            system = GENERAL_SYSTEM.format(lang=lang, today=date.today().isoformat()) + note
            user_msg = question
            context, temp, max_tok = history, cfg.temperature, cfg.max_tokens
            how = "Answering from the model's own knowledge."
        msgs = [{"role": "system", "content": system}, *context, {"role": "user", "content": user_msg}]
        yield step("thought", "Writing the answer", how)
        text, res = "", None
        try:
            for kind, val in iter_chat(llm, msgs, temperature=temp, top_p=cfg.top_p, max_tokens=max_tok):
                if kind == "token":
                    text += val
                    yield {"type": "token", "text": val}
                else:
                    res = val
        except LLMError as exc:
            yield step("error", "Model error", str(exc))
            yield finish(f"The model call failed: {exc}", error=str(exc))
            return
        if res:
            usage.update(prompt_tokens=res.prompt_tokens, completion_tokens=res.completion_tokens, llm_calls=1, llm_ms=res.latency_ms, estimated=res.estimated)
        answer_text = normalise_input(text, final=True).strip() or "I don't know."
        if evidence:
            answer_text = grounding.tidy_citations(answer_text)
        extra: dict = {"route": route}
        abstained = bool(grounding._ABSTAIN.search(answer_text))
        if evidence and abstained:
            extra["grounding"] = {"checked": False, "abstained": True, "repaired": [], "unverified": []}
            if route == "web":
                answer_text += "\n\n*Tip: try rephrasing with exact names, or add the year.*"
        elif evidence:
            answer_text, repaired, unverified = grounding.check_answer(answer_text, evidence, question)
            if repaired:
                yield step("guard", "Spelling check", "Fixed to match the sources: " + ", ".join(f"{a} -> {b}" for a, b in repaired), data={"guard": "grounding"})
            if unverified:
                answer_text += "\n\n*Unverified (not found in the sources): " + ", ".join(unverified) + ".*"
                yield step("guard", "Verification", "Not found in the sources: " + ", ".join(unverified), data={"guard": "grounding"})
            extra["grounding"] = {"checked": True, "repaired": [list(r) for r in repaired], "unverified": unverified}
        if cfg.guardrails:
            safe, events = safety.guard_output(answer_text)
            if events:
                guard_events.append({"guard": "output-guard", "events": events})
                yield step("guard", "Output guard", ", ".join(events), data={"events": events, "before": answer_text[:300]})
            answer_text = safe
        yield step("answer", "Answer", answer_text)
        budget = {"window": cfg.context_window, "used": usage["prompt_tokens"], "system": count_tokens(system), "history": sum(count_tokens(m["content"]) for m in context),
                  "question": count_tokens(question), "retrieved": count_tokens(evidence), "steps": 0, "percent": round(100 * usage["prompt_tokens"] / cfg.context_window, 1)}
        result = finish(answer_text, extra=extra)
        result["budget"] = budget
        yield result

    route, route_reason = choose_route(question, cfg, enabled)
    if route != "agent":
        yield from routed(route, route_reason)
        return

    # ------------------------------------------------------------------ 3. loop
    bad_json = 0
    forced_web = False
    seen_calls: set[tuple[str, str]] = set()
    answer: str | None = None
    for _ in range(cfg.max_steps):
        try:
            res = llm.chat(messages, temperature=cfg.temperature, top_p=cfg.top_p, max_tokens=cfg.max_tokens, json_mode=True)
        except LLMError as exc:
            yield step("error", "Model error", str(exc))
            yield finish(f"The model call failed: {exc}", error=str(exc), messages=messages)
            return
        usage["prompt_tokens"] += res.prompt_tokens
        usage["completion_tokens"] += res.completion_tokens
        usage["llm_calls"] += 1
        usage["llm_ms"] += res.latency_ms
        usage["estimated"] = usage["estimated"] or res.estimated
        meta = {"ms": res.latency_ms, "prompt_tokens": res.prompt_tokens, "completion_tokens": res.completion_tokens}

        action = parse_action(res.text)
        if action is None:
            bad_json += 1
            # Small chat models often just answer in plain text. In general-knowledge mode that is a fine answer.
            if cfg.general_knowledge and res.text.strip() and not tools_used:
                yield step("thought", "Answering directly", "The model replied in plain text, so I'm using it as the answer.", **meta)
                answer = res.text.strip()
                break
            yield step("warn", "Invalid JSON from model", res.text[:300], **meta)
            if bad_json >= 2:
                answer = res.text.strip() or "I don't know."
                break
            messages.append({"role": "assistant", "content": res.text})
            messages.append({"role": "user", "content": 'Reply with ONE valid JSON object only: {"thought": "...", "action": "...", "action_input": "..."}'})
            continue

        thought = str(action.get("thought", "")).strip()
        name = str(action.get("action", "")).strip()
        arg = normalise_input(action.get("action_input", ""), final=(name == "final_answer"))
        if thought:
            yield step("thought", "Thought", thought, **meta)

        if (name == "final_answer" and not tools_used and not forced_web and cfg.general_knowledge
                and "web_search" in enabled and needs_web(question)):
            forced_web = True
            name, arg = "web_search", re.sub(r"\s+", " ", question.strip().rstrip("?")).strip()
            action = {"thought": "This is a factual question, so I should check the web instead of trusting memory.", "action": name, "action_input": arg}
            yield step("guard", "Search-first policy", "Factual question: checking the web before answering, because small models often guess names and dates.", data={"guard": "search-first"})

        if name == "final_answer":
            answer = arg
            break
        if name not in enabled:
            obs_text = f"Unknown or disabled tool '{name}'. Available: {', '.join(enabled)}, final_answer."
            yield step("observation", "Observation", obs_text, tool=name, ok=False)
            messages += [{"role": "assistant", "content": json.dumps(action)}, {"role": "user", "content": f"{OBS_PREFIX}{name}:\n{obs_text}"}]
            continue
        if (name, arg) in seen_calls:
            messages += [
                {"role": "assistant", "content": json.dumps(action)},
                {"role": "user", "content": f"{OBS_PREFIX}{name}:\nYou already ran this exact call. Use final_answer now."},
            ]
            continue
        seen_calls.add((name, arg))

        yield step("tool", f"Call {name}", arg, tool=name, input=arg)
        t_tool = time.time()
        if name == "search_docs":
            out = tools.search_docs(store, arg, registry, k=cfg.top_k, mode=cfg.retrieval, min_score=cfg.min_score, sanitize=cfg.guardrails)
            retrievals.append(out.data)
        elif name == "web_search":
            out = tools.web_search(arg, registry, k=5, sanitize=cfg.guardrails)
        elif name == "run_sql":
            out = tools.run_sql(arg, guard=cfg.guardrails)
        else:
            out = tools.calculator(arg)
        tools_used.append(name)
        guard_events.extend(out.guard_events)
        yield step(
            "observation", "Observation", out.text, tool=name, ok=out.ok, data=out.data,
            ms=int((time.time() - t_tool) * 1000), guards=out.guard_events,
        )
        for ev in out.guard_events:
            if ev.get("guard") in ("context-sanitiser", "sql-guard", "db-readonly"):
                yield step("guard", {"context-sanitiser": "Context sanitiser", "sql-guard": "SQL guard", "db-readonly": "Read-only database"}[ev["guard"]],
                           ev.get("reason") or "Removed instruction-like text from a retrieved passage before the model saw it.", data=ev)
        messages += [{"role": "assistant", "content": json.dumps(action)}, {"role": "user", "content": f"{OBS_PREFIX}{name}:\n{out.text}"}]

    if answer is None:
        answer = "I wasn't able to reach a confident answer within the step limit."
        yield step("warn", "Step limit", f"Stopped after {cfg.max_steps} steps.")

    # ------------------------------------------------------------------ 4. output guard
    if cfg.guardrails:
        safe, events = safety.guard_output(answer)
        if events:
            guard_events.append({"guard": "output-guard", "events": events})
            yield step("guard", "Output guard", ", ".join(events), data={"events": events, "before": answer[:300]})
        answer = safe
    yield step("answer", "Answer", answer)
    yield finish(answer, messages=messages)


def run_agent_sync(question: str, history: list[dict] | None, cfg: AgentConfig, kb: KnowledgeBase) -> tuple[dict, list[dict]]:
    steps: list[dict] = []
    final: dict = {}
    for ev in run_agent(question, history, cfg, kb):
        if ev["type"] == "final":
            final = ev
        elif ev["type"] == "step":
            steps.append(ev)
    return final, steps
