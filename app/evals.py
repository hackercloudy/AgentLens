"""Evaluation runner: scores an agent configuration against the eval set and stores the run for comparison."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import config
from .agent import ABSTAIN_RE, AgentConfig, run_agent_sync
from .evalset import CASES, CATEGORY_LABELS
from .ingest import KnowledgeBase

RUN_DIR = config.EVAL_DIR / "runs"
RUN_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------------- background jobs
@dataclass
class Job:
    id: str
    kind: str
    total: int
    done: int = 0
    status: str = "running"      # running | done | error | cancelled
    result: dict | None = None
    error: str | None = None
    cancel: bool = False
    started: float = field(default_factory=time.time)
    note: str = ""

    def public(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "total": self.total, "done": self.done, "status": self.status,
            "error": self.error, "note": self.note, "elapsed": round(time.time() - self.started, 1),
        }


JOBS: dict[str, Job] = {}


def start_job(kind: str, total: int, target) -> Job:
    job = Job(id=uuid.uuid4().hex[:10], kind=kind, total=total)
    JOBS[job.id] = job

    def runner() -> None:
        try:
            job.result = target(job)
            job.status = "cancelled" if job.cancel else "done"
        except Exception as exc:  # noqa: BLE001
            job.status, job.error = "error", str(exc)

    threading.Thread(target=runner, daemon=True).start()
    return job


# ------------------------------------------------------------------------------------- scoring
def _gold_values(sql: str) -> list:
    con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    try:
        row = con.execute(sql).fetchone()
    finally:
        con.close()
    return list(row) if row else []


def _variants(v) -> list[str]:
    if isinstance(v, str):
        return [v.lower()]
    if v is None:
        return []
    out = {str(v)}
    f = float(v)
    out.add(f"{f:,.2f}")
    out.add(f"{f:.2f}")
    out.add(f"{f:,.2f}".rstrip("0").rstrip("."))
    out.add(f"{f:.2f}".rstrip("0").rstrip("."))
    if f == int(f):
        out.add(f"{int(f):,}")
        out.add(str(int(f)))
    return list(out)


def score_case(case: dict, final: dict) -> dict:
    answer = final.get("answer", "")
    low = answer.lower()
    plain = low.replace(",", "")
    checks: dict[str, bool] = {}

    if case.get("expect_abstain"):
        checks["abstains"] = bool(ABSTAIN_RE.search(answer)) or bool(re.search(r"no data|no matching", low))
        checks["no_hallucinated_number"] = not re.search(r"\d{3,}", plain)
    else:
        if case.get("contains_any"):
            checks["contains_expected"] = any(s.lower() in low for s in case["contains_any"])
        if case.get("gold_sql"):
            gold = _gold_values(case["gold_sql"])
            ok = bool(gold)
            for v in gold:
                vs = _variants(v)
                if vs and not any(x in low or x in plain for x in vs):
                    ok = False
            checks["matches_database"] = ok
            case = {**case, "_gold": [str(v) for v in gold]}
        if case.get("needs_citation"):
            checks["cites_source"] = bool(re.search(r"\[\d+\]", answer))
        tools = final.get("tools_used", [])
        if case.get("expect_tool"):
            checks["used_right_tool"] = case["expect_tool"] in tools
        if case.get("expect_tool2"):
            checks["used_second_tool"] = case["expect_tool2"] in tools
    return {"checks": checks, "passed": bool(checks) and all(checks.values()), "gold": case.get("_gold")}


def label_for(cfg: dict) -> str:
    bits = [cfg.get("model", "mock:offline-demo").split(":", 1)[-1]]
    bits.append("RAG on" if cfg.get("use_docs", True) else "RAG off")
    if cfg.get("chunk_size"):
        bits.append(f"chunk {cfg['chunk_size']}")
    if cfg.get("retrieval", "hybrid") != "hybrid":
        bits.append(cfg["retrieval"])
    if cfg.get("prompt_style", "zero-shot") != "zero-shot":
        bits.append(cfg["prompt_style"])
    if cfg.get("top_k") and int(cfg["top_k"]) != 4:
        bits.append(f"k={cfg['top_k']}")
    return " · ".join(bits)


def run_eval(cfg_dict: dict, kb: KnowledgeBase, job: Job | None = None, case_ids: list[str] | None = None) -> dict:
    cfg = AgentConfig.from_dict({**cfg_dict, "general_knowledge": False, "use_web": False})  # evals must be strict
    cases = [c for c in CASES if not case_ids or c["id"] in case_ids]
    results = []
    t0 = time.time()
    for case in cases:
        if job and job.cancel:
            break
        final, steps = run_agent_sync(case["question"], [], cfg, kb)
        s = score_case(case, final)
        results.append(
            {
                "id": case["id"], "category": case["category"], "question": case["question"], "answer": final.get("answer", ""),
                "passed": s["passed"], "checks": s["checks"], "gold": s["gold"],
                "latency_ms": final.get("latency_ms", 0), "tokens": final["usage"]["total_tokens"] if final.get("usage") else 0,
                "tools_used": final.get("tools_used", []), "error": final.get("error"),
            }
        )
        if job:
            job.done += 1
            job.note = case["question"]
    n = max(1, len(results))
    by_cat: dict[str, dict] = {}
    for r in results:
        c = by_cat.setdefault(r["category"], {"label": CATEGORY_LABELS.get(r["category"], r["category"]), "passed": 0, "total": 0})
        c["total"] += 1
        c["passed"] += int(r["passed"])
    check_totals: dict[str, list[int]] = {}
    for r in results:
        for k, v in r["checks"].items():
            t = check_totals.setdefault(k, [0, 0])
            t[0] += int(v)
            t[1] += 1
    run = {
        "id": uuid.uuid4().hex[:8],
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "label": label_for(cfg.public()),
        "config": cfg.public(),
        "accuracy": round(100 * sum(r["passed"] for r in results) / n, 1),
        "passed": sum(r["passed"] for r in results),
        "total": len(results),
        "by_category": by_cat,
        "checks": {k: {"passed": v[0], "total": v[1]} for k, v in check_totals.items()},
        "avg_latency_ms": int(sum(r["latency_ms"] for r in results) / n),
        "avg_tokens": int(sum(r["tokens"] for r in results) / n),
        "wall_seconds": round(time.time() - t0, 1),
        "results": results,
    }
    (RUN_DIR / f"{run['id']}.json").write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")
    return run


def list_runs() -> list[dict]:
    runs = []
    for p in sorted(RUN_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        runs.append({k: r[k] for k in ("id", "created", "label", "accuracy", "passed", "total", "avg_latency_ms", "avg_tokens", "by_category", "config")})
    return runs


def get_run(run_id: str) -> dict | None:
    p = RUN_DIR / f"{re.sub(r'[^a-z0-9]', '', run_id)}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def delete_run(run_id: str) -> bool:
    p = RUN_DIR / f"{re.sub(r'[^a-z0-9]', '', run_id)}.json"
    if p.exists():
        p.unlink()
        return True
    return False
