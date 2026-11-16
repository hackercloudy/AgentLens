"""Tools the agent can call. Each returns plain text for the model plus structured data for the UI."""
from __future__ import annotations

import ast
import html as _html
import math
import operator
import re
import sqlite3
from urllib.parse import urlparse
from dataclasses import dataclass, field

from . import config, safety
from .store import VectorStore

MAX_ROWS = 25


@dataclass
class ToolResult:
    text: str                       # what the model sees
    data: dict = field(default_factory=dict)   # what the UI sees
    ok: bool = True
    guard_events: list[dict] = field(default_factory=list)


# ------------------------------------------------------------------------------- search_docs
def clean_for_model(text: str) -> str:
    """Turn markdown into plain sentences: headings become short sentences, notice banners are dropped."""
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(">"):
            continue
        if line.startswith("#"):
            out.append(line.lstrip("# ").strip().rstrip(".") + ".")
        elif line.startswith("- "):
            out.append("\u2022 " + line[2:].strip())
        else:
            out.append(line)
    return " ".join(out)


class SourceRegistry:
    """Keeps citation numbers stable across the steps of one agent run."""

    def __init__(self) -> None:
        self.items: list[dict] = []
        self._index: dict[str, int] = {}

    def register(self, hit: dict) -> int:
        if hit["id"] not in self._index:
            self.items.append(hit)
            self._index[hit["id"]] = len(self.items)
        return self._index[hit["id"]]


def search_docs(
    store: VectorStore,
    query: str,
    registry: SourceRegistry,
    k: int = 4,
    mode: str = "hybrid",
    min_score: float = 0.12,
    sanitize: bool = True,
) -> ToolResult:
    hits = store.search(query, k=k, mode=mode)
    shown = [h for h in hits if h.score >= min_score]
    data = {"query": query, "mode": mode, "k": k, "min_score": min_score, "hits": [h.to_dict() for h in hits]}
    events: list[dict] = []
    if not shown:
        best = hits[0].score if hits else 0.0
        return ToolResult(
            f"No relevant passages found (best match score {best:.2f}, below threshold {min_score}). "
            "The documents do not appear to contain this information.",
            data,
            guard_events=events,
        )
    lines = []
    for h in shown:
        d = h.to_dict()
        text = clean_for_model(d["text"])
        if sanitize:
            text, removed = safety.sanitize_context(text)
            if removed:
                events.append({"guard": "context-sanitiser", "chunk": d["id"], "removed": [f.to_dict() for f in removed]})
                d["sanitised"] = True
        d["text_for_model"] = text
        n = registry.register(d)
        where = f"doc: {d['doc']}" + (f", p.{d['page']}" if d["page"] else "")
        lines.append(f"[{n}] ({where}) {' '.join(text.split())}")
    return ToolResult("\n".join(lines), data, guard_events=events)


# ------------------------------------------------------------------------------------ web_search
UA = "AgentLens/1.0 (educational project; https://github.com/hackercloudy/AgentLens)"


_Q_STOP = set("when where who whom whose what which how why will would shall should can could is are was were be been do does did the a an of in on at to for from by with and or about tell me please give begin begins start starts starting".split())


def _keywords(query: str) -> str:
    """'when will the next odi world cup begin' -> 'next odi world cup' (better for encyclopedia-style search)."""
    words = [w for w in re.findall(r"[A-Za-z0-9']+", query) if w.lower() not in _Q_STOP]
    return " ".join(words) or query


def _ddg_search(query: str, k: int) -> list[dict]:
    """Try several search backends in turn; the first one that returns results wins (individual backends get rate-limited)."""
    import time as _time

    try:
        from ddgs import DDGS
    except ImportError:  # older package name
        from duckduckgo_search import DDGS  # type: ignore
    deadline, last = _time.time() + 25, None
    for backend in ("auto", "bing", "brave", "duckduckgo", "yahoo"):
        if _time.time() > deadline:
            break
        try:
            rows = list(DDGS(timeout=8).text(query, max_results=k, backend=backend))
        except Exception as exc:  # noqa: BLE001
            last = exc
            continue
        if rows:
            return [{"title": r.get("title", ""), "url": r.get("href") or r.get("url", ""), "snippet": r.get("body") or r.get("snippet", "")} for r in rows]
    if last:
        raise last
    return []


def _wiki_search(query: str, k: int) -> list[dict]:
    import httpx

    r = httpx.get(
        "https://en.wikipedia.org/w/api.php",
        params={
            "action": "query", "generator": "search", "gsrsearch": _keywords(query), "gsrlimit": k, "prop": "extracts|info",
            "exintro": 1, "explaintext": 1, "exsentences": 4, "inprop": "url", "format": "json", "formatversion": 2,
        },
        headers={"User-Agent": UA}, timeout=10,
    )
    r.raise_for_status()
    pages = sorted(r.json().get("query", {}).get("pages", []), key=lambda p: p.get("index", 99))
    return [{"title": p.get("title", ""), "url": p.get("fullurl", ""), "snippet": p.get("extract", "")} for p in pages if p.get("extract")]


def _clean_snippet(text: str, limit: int = 600) -> str:
    text = re.sub(r"<[^>]+>", " ", _html.unescape(text or ""))
    text = " ".join(text.split())
    return text[:limit] + ("..." if len(text) > limit else "")


def web_search(query: str, registry: SourceRegistry, k: int = 5, sanitize: bool = True) -> ToolResult:
    """Public web search. Results are untrusted text: they are sanitised like retrieved documents."""
    results: list[dict] = []
    provider, errors = "", []
    for name, fn in (("duckduckgo", _ddg_search), ("wikipedia", _wiki_search)):
        try:
            results = fn(query, k)
        except Exception as exc:  # noqa: BLE001 - network problems must never crash the agent
            errors.append(f"{name}: {str(exc)[:120]}")
            results = []
        if results:
            provider = name
            break
    if not results:
        msg = "Web search is unavailable or found nothing (no internet, or the search service blocked the request)."
        return ToolResult(msg + " Answer from your own knowledge and say it may be out of date.", {"query": query, "results": [], "errors": errors}, ok=False)
    lines, shown, events = [], [], []
    for r in results[:k]:
        url = r.get("url", "")
        domain = urlparse(url).netloc.removeprefix("www.") or "web"
        snippet = _clean_snippet(r.get("snippet", ""))
        if sanitize:
            snippet, removed = safety.sanitize_context(snippet)
            if removed:
                events.append({"guard": "context-sanitiser", "chunk": url, "removed": [f.to_dict() for f in removed]})
        item = {"id": url or f"web:{len(registry.items)}", "doc": domain, "page": 0, "heading": r.get("title", "")[:120], "text": snippet,
                "score": 1.0, "dense": 0.0, "bm25": 0.0, "rank": len(shown) + 1, "kind": "web", "url": url, "text_for_model": snippet,
                "sanitised": bool(events and events[-1]["chunk"] == url)}
        n = registry.register(item)
        lines.append(f"[{n}] ({domain}) {item['heading']}: {snippet}")
        shown.append({"n": n, "title": item["heading"], "url": url, "domain": domain, "snippet": snippet[:200]})
    return ToolResult("\n".join(lines), {"query": query, "provider": provider, "results": shown}, guard_events=events)


# ------------------------------------------------------------------------------------ run_sql
def _connect_readonly() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    con.execute("PRAGMA query_only = ON")
    return con


def get_schema() -> str:
    con = _connect_readonly()
    try:
        out = []
        for name, kind, sql in con.execute("SELECT name, type, sql FROM sqlite_master WHERE type IN ('table','view') ORDER BY type DESC, name"):
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({name})")]
            out.append(f"{kind.upper()} {name}({', '.join(cols)})")
        return "\n".join(out)
    finally:
        con.close()


def schema_details() -> dict:
    con = _connect_readonly()
    try:
        items = []
        for name, kind in con.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table','view') ORDER BY type DESC, name").fetchall():
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({name})")]
            rows = con.execute(f"SELECT * FROM {name} LIMIT 4").fetchall()
            count = con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            items.append({"name": name, "kind": kind, "columns": cols, "rows": rows, "count": count})
        return {"tables": items}
    finally:
        con.close()


def run_sql(sql: str, guard: bool = True) -> ToolResult:
    events: list[dict] = []
    if guard:
        ok, reason = safety.check_sql(sql)
        if not ok:
            events.append({"guard": "sql-guard", "reason": reason})
            return ToolResult(f"SQL rejected by the safety guard: {reason}", {"sql": sql, "error": reason, "blocked": True}, ok=False, guard_events=events)
    con = _connect_readonly()  # defence in depth: even with the guard off the DB is read-only
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(MAX_ROWS + 1)
        truncated = len(rows) > MAX_ROWS
        rows = rows[:MAX_ROWS]
    except sqlite3.Error as exc:
        layer = "database read-only mode" if "readonly" in str(exc).lower() or "query_only" in str(exc).lower() else "database"
        events.append({"guard": "db-readonly" if layer.startswith("database read") else "db-error", "reason": str(exc)})
        return ToolResult(f"SQL error: {exc}", {"sql": sql, "error": str(exc), "blocked": layer.startswith("database read")}, ok=False, guard_events=events)
    finally:
        con.close()
    header = " | ".join(cols)
    body = "\n".join(" | ".join("" if v is None else str(v) for v in r) for r in rows)
    text = f"{header}\n{body}" if rows else f"{header}\n(0 rows)"
    if truncated:
        text += f"\n(showing first {MAX_ROWS} rows)"
    return ToolResult(text, {"sql": sql, "columns": cols, "rows": rows, "truncated": truncated}, guard_events=events)


# ------------------------------------------------------------------------------- calculator
_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Pow: operator.pow, ast.Mod: operator.mod, ast.USub: operator.neg, ast.UAdd: operator.pos,
    ast.FloorDiv: operator.floordiv,
}
_FUNCS = {"round": round, "min": min, "max": max, "abs": abs, "sqrt": math.sqrt, "log": math.log, "sum": sum}


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("Exponent too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
        return _FUNCS[node.func.id](*[_eval(a) for a in node.args])
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e) for e in node.elts]  # type: ignore[return-value]
    raise ValueError("Unsupported expression")


def calculator(expression: str) -> ToolResult:
    try:
        expr = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", expression.replace("^", "**"))
        value = _eval(ast.parse(expr.strip(), mode="eval"))
    except Exception as exc:  # noqa: BLE001 - tool must never crash the agent
        return ToolResult(f"Calculator error: {exc}", {"expression": expression, "error": str(exc)}, ok=False)
    shown = round(value, 6) if isinstance(value, float) else value
    return ToolResult(f"{expression} = {shown}", {"expression": expression, "value": shown})


TOOL_SPECS = {
    "search_docs": 'search_docs(query: string): semantic + keyword search over the uploaded documents (policies, guides, SOPs). Returns numbered passages like [1], [2].',
    "run_sql": "run_sql(sql: string): run ONE read-only SQLite SELECT against the sales database. Use the `sales` view for revenue questions.",
    "calculator": "calculator(expression: string): evaluate arithmetic such as (120*42)*(1-0.1).",
    "web_search": "web_search(query: string): search the public web for recent or factual information. Returns numbered results like [3].",
}
