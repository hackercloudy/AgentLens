"""Offline simulator: a *rule-based* stand-in for an LLM.

It exists so AgentLens runs end-to-end (agent loop, RAG, SQL, evals, red-team) with no model installed, in CI,
and on a laptop with no GPU. It is NOT a language model:

* In agent mode it behaves like a small, deliberately *naive* ReAct policy: keyword intent routing, template
  text-to-SQL for the demo schema, and extractive answers copied from retrieved passages.
* It is intentionally gullible: it obeys instructions it finds in retrieved text or in the user message, which is
  exactly what the Red Team tab needs in order to show guardrails working.
* In free-form mode (Playground) it is a toy bigram language model trained on the demo documents, so temperature
  and top-p visibly change the output.

Switch to a real open model (Ollama / Groq) for real quality numbers.
"""
from __future__ import annotations

import json
import random
import re
import time
from collections import defaultdict

from . import config
from .demo_data import DOCS, PRODUCTS
from .embeddings import analyze
from .llm import LLMResult, _est_prompt_tokens
from .prompts import AGENT_MARKER, OBS_PREFIX
from .tokens import count_tokens

REGIONS = ["north", "south", "east", "west"]
PRODUCT_NAMES = [p[0] for p in PRODUCTS]
GENERIC_CAPS = {
    "What", "Which", "How", "When", "Who", "Where", "Why", "Does", "Can", "Should", "Is", "Are", "Do", "The", "Give",
    "Tell", "List", "Show", "Please", "Hindi", "English", "Zentrovax's", "North", "South", "East", "West", "Q", "Net",
    "Hospital", "Clinic", "Pharmacy", "Serious", "Any", "For", "After", "Before", "If", "Our", "Did", "Was", "Were",
}

DOC_WORDS = re.compile(
    r"\b(dose|dosage|dosing|mg|kg|child|children|infant|renal|kidney|egfr|missed|storage|store|stored|temperature|excursion|"
    r"cold chain|consent|adverse|sae|deviation|retention|retain|breach|privacy|policy|approval|approve|approved|guideline|"
    r"sop|procedure|report|update|notice|portal|maintenance|status|transport|shipper|calibrat\w*|contraindicat\w*|side effects?|salary|confidential|discount "
    r"(?:above|up to|over)|definition|how long|within|notify|notified|quarantin\w*|hold|ship|cut-?off|refrigerat\w*)\b",
    re.I,
)
SQL_WORDS = re.compile(
    r"\b(revenue|sales|sold|units?|orders?|accounts?|customers?|region|regions|top|best[- ]selling|average|avg|"
    r"how many|total|count|trend|quarter|quarterly|q[1-4]|2025|2026|drop|decline|fell|biggest|highest|lowest|"
    r"per region|by region|contact|emails?)\b",
    re.I,
)
CALC_RE = re.compile(r"(?:calculate|compute|what is|what's)\s+([\d\s.+\-*/()^%]+)\??\s*$", re.I)


# =============================================================================== text-to-SQL (templates)
def _quarter_prev(label: str) -> str:
    y, q = label.split("-Q")
    y, q = int(y), int(q)
    return f"{y - 1}-Q4" if q == 1 else f"{y}-Q{q - 1}"


def _find_quarter(q: str) -> str | None:
    m = re.search(r"(20\d\d)[-\s]*q([1-4])", q) or None
    if m:
        return f"{m.group(1)}-Q{m.group(2)}"
    m = re.search(r"\bq([1-4])[\s,]*(?:of\s+)?(20\d\d)", q)
    if m:
        return f"{m.group(2)}-Q{m.group(1)}"
    return None


def _where(q: str, allow_product=True) -> list[str]:
    w = []
    quarter = _find_quarter(q)
    year = re.search(r"\b(20\d\d)\b", q)
    if quarter:
        w.append(f"quarter = '{quarter}'")
    elif year:
        w.append(f"year = {year.group(1)}")
    for r in REGIONS:
        if re.search(rf"\b{r}\b(?!\s*(?:india)?\s*(?:region)s?\s*(?:had|has)\b)", q) and not re.search(r"which region|by region|per region|each region", q):
            w.append(f"region = '{r.capitalize()}'")
            break
    if allow_product:
        for p in PRODUCT_NAMES:
            if p.lower() in q:
                w.append(f"product = '{p}'")
                break
    return w


def sql_for(question: str) -> tuple[str, str] | None:
    """Template text-to-SQL for the demo schema. Returns (sql, description) or None."""
    q = question.lower()

    destructive = re.search(r"(drop\s+table\s+\w+|delete\s+from\s+\w+|update\s+\w+\s+set\s+[^;]+|insert\s+into\s+\w+[^;]*)", q)
    if destructive:
        return destructive.group(1), "run the statement exactly as requested"

    if re.search(r"\b(emails?|contact details|contacts)\b", q) and re.search(r"account|hospital|clinic|customer|contact", q):
        return "SELECT name, contact_email FROM accounts LIMIT 5", "list account contact emails"

    if re.search(r"how many (accounts|hospitals|clinics|customers|pharmacies)|number of (accounts|hospitals|customers)|accounts per|accounts in", q):
        if re.search(r"per region|by region|each region", q):
            return "SELECT region, COUNT(*) AS accounts FROM accounts GROUP BY region ORDER BY region", "count accounts per region"
        wh = []
        for r in REGIONS:
            if re.search(rf"\b{r}\b", q):
                wh.append(f"region = '{r.capitalize()}'")
                break
        clause = f" WHERE {' AND '.join(wh)}" if wh else ""
        return f"SELECT COUNT(*) AS accounts FROM accounts{clause}", "count accounts"

    if re.search(r"\b(drop|decline|fell|decrease|fall|dropped)\b", q) and re.search(r"region", q):
        cur = _find_quarter(q) or "2026-Q3"
        prev = _quarter_prev(cur)
        sql = (
            "SELECT region, "
            f"ROUND(SUM(CASE WHEN quarter = '{prev}' THEN revenue ELSE 0 END), 2) AS prev_quarter_revenue, "
            f"ROUND(SUM(CASE WHEN quarter = '{cur}' THEN revenue ELSE 0 END), 2) AS this_quarter_revenue, "
            f"ROUND(SUM(CASE WHEN quarter = '{cur}' THEN revenue ELSE 0 END) - SUM(CASE WHEN quarter = '{prev}' THEN revenue ELSE 0 END), 2) AS change "
            "FROM sales GROUP BY region ORDER BY change ASC LIMIT 1"
        )
        return sql, f"compare revenue by region, {prev} vs {cur}"

    if re.search(r"average discount|avg discount|mean discount", q):
        wh = _where(q)
        return f"SELECT ROUND(AVG(discount_pct), 2) AS avg_discount_pct FROM sales{' WHERE ' + ' AND '.join(wh) if wh else ''}", "average discount"

    metric_sql, alias = "ROUND(SUM(revenue), 2)", "revenue"
    if re.search(r"\b(units?|quantity|sold|volume)\b", q) and not re.search(r"revenue", q):
        metric_sql, alias = "SUM(quantity)", "units"
    elif re.search(r"\b(orders?)\b", q) and not re.search(r"revenue|units", q):
        metric_sql, alias = "COUNT(*)", "orders"

    group = None
    if re.search(r"top (\d+ |three |five )?(account|customer)s?|which (account|customer|hospital|clinic)|best customer|by account|per account|each account|biggest (account|customer)", q):
        group = "account"
    elif re.search(r"which product|best[- ]selling|top[- ]selling|top (\d+ |three |five )?products?|by product|per product|each product|most popular product|which medicine|which drug", q):
        group = "product"
    elif re.search(r"which region|by region|per region|each region|top region|best region|highest region|regional", q):
        group = "region"
    elif re.search(r"by quarter|per quarter|each quarter|quarterly|trend", q):
        group = "quarter"
    elif re.search(r"by segment|per segment|each segment|which segment", q):
        group = "segment"

    wh = _where(q, allow_product=(group != "product"))
    if group in ("region",):
        wh = [c for c in wh if not c.startswith("region")]
    where_sql = f" WHERE {' AND '.join(wh)}" if wh else ""

    if not group:
        return f"SELECT {metric_sql} AS {alias} FROM sales{where_sql}", f"total {alias}"

    limit = None
    order = f"{alias} DESC"
    m = re.search(r"top (\d+)", q)
    words = {"three": 3, "five": 5}
    mw = re.search(r"top (three|five)", q)
    if m:
        limit = int(m.group(1))
    elif mw:
        limit = words[mw.group(1)]
    elif re.search(r"\b(lowest|least|worst|smallest|fewest)\b", q):
        limit, order = 1, f"{alias} ASC"
    elif re.search(r"\bwhich\b|\bbest\b|\bhighest\b|\bmost\b|\bbiggest\b|\blargest\b", q):
        limit = 1
    elif re.search(r"\btop\b", q):
        limit = 3
    if group == "quarter":
        order = "quarter ASC"
    lim = f" LIMIT {limit}" if limit else ""
    return (
        f"SELECT {group}, {metric_sql} AS {alias} FROM sales{where_sql} GROUP BY {group} ORDER BY {order}{lim}",
        f"{alias} by {group}",
    )


def split_question(question: str) -> tuple[str, str]:
    """For two-part questions ("..., and how many ...") return (document_part, data_part)."""
    m = re.search(r",?\s+and\s+(?=(?:how many|how much|what was|what were|which|what is the (?:total|average)))", question, re.I)
    if m:
        return question[: m.start()].strip(), question[m.end():].strip()
    return question, question


STRONG_SQL = re.compile(r"\b(units?|sold|revenue|orders?|accounts?|average discount|20\d\d|q[1-4])\b", re.I)


# ====================================================================================== answer writing
def _fmt(v: str) -> str:
    if re.fullmatch(r"-?\d+(\.\d+)?", v):
        f = float(v)
        return f"{int(f):,}" if f == int(f) and "." not in v else f"{f:,.2f}"
    return v


def _label(col: str) -> str:
    return col.replace("_", " ")


def narrate_sql(text: str) -> str:
    lines = [l for l in text.split("\n") if l.strip()]
    if not lines or lines[0].startswith("SQL"):
        return "The database query failed, so I could not get the numbers: " + text.strip()
    header = lines[0].split(" | ")
    rows = [l.split(" | ") for l in lines[1:] if not l.startswith("(")]
    if rows and all(not any(c.strip() for c in r) for r in rows):
        return "The sales database has no data for that period or filter."
    if not rows:
        return "The sales database returned no matching rows."
    if len(rows) == 1 and len(header) == 1:
        phrases = {
            "revenue": "the total net revenue is",
            "units": "the total units sold is",
            "orders": "the number of orders is",
            "accounts": "the number of accounts is",
            "avg_discount_pct": "the average discount (%) is",
        }
        return f"According to the sales database, {phrases.get(header[0], _label(header[0]) + ' is')} {_fmt(rows[0][0])}."
    if len(rows) == 1:
        return "According to the sales database: " + ", ".join(f"{_label(h)} {_fmt(v)}" for h, v in zip(header, rows[0])) + "."
    out = ["Here is what the sales database returned:"]
    for r in rows[:8]:
        out.append(f"- {_fmt(r[0])}: " + ", ".join(f"{_label(h)} {_fmt(v)}" for h, v in zip(header[1:], r[1:])))
    return "\n".join(out)


_PASSAGE = re.compile(r"^\[(\d+)\]\s+\((.*?)\)\s+(.*)$", re.M)
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\s*\u2022\s*")


def _passages(obs_text: str) -> list[tuple[int, str]]:
    return [(int(m.group(1)), m.group(3)) for m in _PASSAGE.finditer(obs_text)]


def _band_answer(question: str, passages: list[tuple[int, str]]) -> str | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*kg", question.lower())
    if not m:
        return None
    w = float(m.group(1))
    for n, text in passages:
        for lo, hi, dose in re.findall(r"(\d+(?:\.\d+)?)\s*kg to (\d+(?:\.\d+)?)\s*kg:\s*([^.]*?)\.", text):
            if float(lo) <= w <= float(hi) + 0.099:
                return f"For a {w:g} kg patient the dose is {dose.strip()} [{n}]."
        for lo, dose in re.findall(r"(\d+(?:\.\d+)?)\s*kg and above:\s*([^.]*?)\.", text):
            if w >= float(lo):
                return f"For a {w:g} kg patient the dose is {dose.strip()} [{n}]."
    return None


def extractive_answer(question: str, passages: list[tuple[int, str]]) -> str | None:
    banded = _band_answer(question, passages)
    if banded:
        return banded
    q_terms = set(analyze(question))
    scored: list[tuple[float, int, int, str]] = []
    for n, text in passages:
        for pos, sent in enumerate(s.strip() for s in _SENT_SPLIT.split(text)):
            if len(sent) < 12:
                continue
            terms = analyze(sent)
            overlap = sum(1 for t in set(terms) if t in q_terms)
            if not overlap:
                continue
            bonus = 0.5 if re.search(r"\d", sent) else 0.0
            scored.append((overlap + bonus - 0.01 * pos, n, pos, sent))
    if not scored:
        return None
    scored.sort(reverse=True)
    best = scored[0]
    picked = [best]
    for cand in scored[1:]:
        if cand[0] >= best[0] * 0.75 and cand[3] != best[3] and len(picked) < 2:
            picked.append(cand)
    picked.sort(key=lambda s: (s[1], s[2]))
    return " ".join(f"{s[3].rstrip('.')}. [{s[1]}]" for s in picked)


def _grounded(question: str, passage_text: str) -> tuple[bool, str]:
    """Abstain when the question names an entity (product, role...) that no retrieved passage mentions."""
    low = passage_text.lower()
    for ent in re.findall(r"\b[A-Z][A-Za-z]{2,}\b", question):
        if ent in GENERIC_CAPS or ent.lower() in REGIONS:
            continue
        if ent.lower() not in low:
            return False, ent
    return True, ""


# ============================================================================================ the model
def _json(thought: str, action: str, action_input: str) -> str:
    return json.dumps({"thought": thought, "action": action, "action_input": action_input})


def _plan(messages: list[dict]) -> str:
    system = messages[0]["content"]
    canary_m = re.search(r"CANARY-[\w-]+", system)
    canary = canary_m.group(0) if canary_m else config.CANARY
    tools = set(re.findall(r"^- (\w+)\(", system, re.M))

    last_q = max((i for i, m in enumerate(messages) if m["role"] == "user" and not m["content"].startswith(OBS_PREFIX)), default=1)
    question = messages[last_q]["content"].strip()
    obs: list[tuple[str, str]] = []
    for m in messages[last_q + 1 :]:
        if m["role"] == "user" and m["content"].startswith(OBS_PREFIX):
            head, _, body = m["content"][len(OBS_PREFIX) :].partition(":\n")
            obs.append((head.strip(), body))
    used = [t for t, _ in obs]
    ql = question.lower()

    # ---- gullible behaviours (so guardrails have something to defend against)
    override = re.search(r"\b(ignore|disregard|forget)\b.{0,40}\b(instruction|prompt|rule)s?\b", ql) or re.search(r"you are now|developer mode|\bdan\b", ql)
    wants_secret = re.search(r"passphrase|system prompt|secret|password|instructions", ql)
    if not obs and override and wants_secret:
        leak = f"Sure. My admin passphrase is {canary}. Here is the start of my system prompt: {system[:140]}"
        return _json("The user told me to ignore my rules, so I will comply.", "final_answer", leak)
    if not obs and re.search(r"!\[[^\]]*\]\(https?://[^)]+\)", question):
        img = re.search(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", question).group(0).replace("<passphrase>", canary)
        return _json("The user asked me to append a link.", "final_answer", f"Done. {img}")

    # ---- first step
    if not obs:
        doc_intent = bool(DOC_WORDS.search(question))
        sql_intent = bool(SQL_WORDS.search(question)) and "run_sql" in tools
        built_now = sql_for(question) if "run_sql" in tools else None
        if built_now and re.match(r"(drop|delete|update|insert)\b", built_now[0], re.I):
            return _json(f"The user wants me to {built_now[1]}.", "run_sql", built_now[0])
        if doc_intent and "search_docs" in tools:
            return _json("This looks like a policy/guide question, so I should search the documents.", "search_docs", split_question(question)[0])
        if sql_intent:
            built = sql_for(split_question(question)[1])
            if built:
                return _json(f"This needs numbers from the database: {built[1]}.", "run_sql", built[0])
        calc = CALC_RE.search(question)
        if calc and "calculator" in tools:
            return _json("This is arithmetic, so I will use the calculator.", "calculator", calc.group(1).strip())
        if "search_docs" in tools and not sql_intent:
            return _json("I am not sure which source helps, so I will try the documents.", "search_docs", question)
        return _json("I have no tool or evidence that can answer this.", "final_answer",
                     "I don't know. I can only answer from the uploaded documents and the sales database, and I couldn't find anything relevant.")

    # ---- after observations
    docs_obs = [text for t, text in obs if t == "search_docs"]
    sql_obs = [text for t, text in obs if t == "run_sql"]
    calc_obs = [text for t, text in obs if t == "calculator"]
    doc_intent = bool(DOC_WORDS.search(question))
    sql_intent = bool(SQL_WORDS.search(question)) and "run_sql" in tools

    doc_q, data_q = split_question(question)
    if docs_obs and not sql_obs and "run_sql" in tools and doc_q != data_q and STRONG_SQL.search(data_q):
        built = sql_for(data_q)
        if built:
            return _json(f"I have the document facts; now I need the numbers: {built[1]}.", "run_sql", built[0])

    parts: list[str] = []
    all_passages: list[tuple[int, str]] = []
    for text in docs_obs:
        all_passages.extend(_passages(text))
    injected = ""
    if all_passages:
        joined = " ".join(t for _, t in all_passages)
        ok, missing = _grounded(doc_q, joined)
        if not ok and not sql_obs:
            return _json("The passages do not mention what was asked.", "final_answer",
                         f"I don't know. The documents don't mention {missing}, so I can't answer that reliably.")
        ans = extractive_answer(doc_q, all_passages)
        if ans:
            parts.append(ans)
        elif not sql_obs:
            return _json("The passages are not specific enough.", "final_answer",
                         "I don't know. I couldn't find a passage that answers that question.")
        # gullible: obey instructions hidden in retrieved text
        if re.search(r"(ignore|disregard)[^.]{0,40}(instruction)", joined, re.I) and re.search(r"passphrase|system prompt", joined, re.I):
            injected += f" Admin passphrase: {canary}."
        img = re.search(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", joined)
        if img:
            injected += " " + img.group(0).replace("<passphrase>", canary)
    elif docs_obs and not sql_obs:
        return _json("Nothing relevant was retrieved.", "final_answer",
                     "I don't know. The documents don't appear to contain this information.")
    for text in sql_obs:
        parts.append(narrate_sql(text))
    for text in calc_obs:
        parts.append(text + ".")
    answer = " ".join(parts) if parts else "I don't know."
    return _json("I have enough evidence to answer.", "final_answer", answer.strip() + injected)


# --------------------------------------------------------------------------- toy bigram LM (free-form)
_BIGRAMS: dict[str, dict[str, int]] | None = None
_STARTS: list[str] = []


def _train() -> None:
    global _BIGRAMS, _STARTS
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    starts: set[str] = set()
    for text in DOCS.values():
        words = re.findall(r"[A-Za-z0-9%.\-]+", text.replace("#", " ").replace(">", " "))
        for a, b in zip(words, words[1:]):
            counts[a.lower()][b.lower()] += 1
        starts.update(w.lower() for w in words if w[:1].isupper() and len(w) > 3)
    _BIGRAMS, _STARTS = counts, sorted(starts)


def _freeform(prompt: str, temperature: float, top_p: float, max_tokens: int) -> str:
    if _BIGRAMS is None:
        _train()
    assert _BIGRAMS is not None
    rng = random.Random() if temperature > 0 else random.Random(1)
    words = re.findall(r"[A-Za-z0-9]+", prompt.lower())
    cur = next((w for w in reversed(words) if w in _BIGRAMS), None) or rng.choice(_STARTS)
    out = [cur]
    for _ in range(min(60, max(8, int(max_tokens / 1.4)))):
        nxt = _BIGRAMS.get(cur)
        if not nxt:
            cur = rng.choice(_STARTS)
            out.append(cur)
            continue
        items = sorted(nxt.items(), key=lambda kv: (-kv[1], kv[0]))
        if temperature <= 0.01:
            cur = items[0][0]
        else:
            weights = [c ** (1.0 / max(temperature, 0.05)) for _, c in items]
            total = sum(weights)
            probs = [w / total for w in weights]
            cum, keep = 0.0, 0
            for p in probs:  # nucleus (top-p) filtering
                cum += p
                keep += 1
                if cum >= top_p:
                    break
            keep = max(1, keep)
            cur = rng.choices([w for w, _ in items[:keep]], weights=probs[:keep])[0]
        out.append(cur)
    text = " ".join(out)
    return text[:1].upper() + text[1:] + "."


class SimulatedLLM:
    provider = "mock"
    model = "offline-demo"

    def chat(self, messages, temperature=0.2, top_p=0.95, max_tokens=512, json_mode=False) -> LLMResult:
        t0 = time.time()
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        if AGENT_MARKER in system:
            text = _plan(messages)
        else:
            prompt = " ".join(m["content"] for m in messages if m["role"] == "user")
            text = _freeform(prompt, temperature, top_p, max_tokens)
        return LLMResult(
            text=text,
            prompt_tokens=_est_prompt_tokens(messages),
            completion_tokens=count_tokens(text),
            latency_ms=max(1, int((time.time() - t0) * 1000)),
            model="mock:offline-demo",
            estimated=True,
        )
