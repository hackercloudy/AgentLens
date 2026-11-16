"""AgentLens API + static web UI.  Run:  uvicorn app.main:app --reload"""
from __future__ import annotations

import asyncio
import base64
import collections
import json
import os
import secrets
from contextlib import asynccontextmanager
import queue
import threading
import time

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, demo_data, evals, redteam, tools
from .agent import AgentConfig, run_agent, run_agent_sync
from .ingest import KnowledgeBase
from .llm import LLMError, get_llm, list_models
from .tokens import count_tokens, tokenize

VERSION = "1.0.0"

# ---- deployment protections (all optional, off by default so local use is unchanged)
PASSWORD = os.getenv("AGENTLENS_PASSWORD", "")                       # HTTP Basic password for the whole site
READONLY = os.getenv("AGENTLENS_READONLY", "").lower() in ("1", "true", "yes")   # public demo: no uploads/index changes; jobs use the simulator
RATE_LIMIT = int(os.getenv("AGENTLENS_RATE_LIMIT", "0") or 0)        # model-calling requests per minute per visitor (0 = off)
_LIMITED = {"/api/chat", "/api/chat/stream", "/api/playground", "/api/evals/run", "/api/redteam/run"}
_hits: dict[str, collections.deque] = {}


def _writable() -> None:
    if READONLY:
        raise HTTPException(403, "This public demo is read-only. Run AgentLens locally to upload documents and change the index.")

kb = KnowledgeBase()


@asynccontextmanager
async def lifespan(_: FastAPI):
    demo_data.ensure_demo_data()
    kb.store()  # warm the index
    yield


app = FastAPI(title="AgentLens", version=VERSION, description="Open-source LLM agent & RAG workbench", lifespan=lifespan)


@app.middleware("http")
async def protect(request: Request, call_next):
    path = request.url.path
    if PASSWORD and path != "/api/health":
        ok = False
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                ok = secrets.compare_digest(pw, PASSWORD)
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            return JSONResponse(status_code=401, content={"detail": "Password required."}, headers={"WWW-Authenticate": 'Basic realm="AgentLens"'})
    if RATE_LIMIT and request.method == "POST" and path in _LIMITED:
        ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "?")
        q = _hits.setdefault(ip, collections.deque())
        now = time.time()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            return JSONResponse(status_code=429, content={"detail": f"Too many requests ({RATE_LIMIT} per minute). Please wait a moment."})
        q.append(now)
    return await call_next(request)


# ------------------------------------------------------------------------------------ models
class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    history: list[dict] = []
    config: dict = {}


class PlaygroundRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    system: str = ""
    model: str = "mock:offline-demo"
    temperature: float = 0.7
    top_p: float = 0.95
    max_tokens: int = 160
    n: int = Field(default=3, ge=1, le=5)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    k: int = 5
    mode: str = "hybrid"
    chunk_size: int | None = None
    embedder: str | None = None


class ReindexRequest(BaseModel):
    chunk_size: int = Field(default=config.DEFAULT_CHUNK_SIZE, ge=150, le=3000)
    overlap: int = Field(default=config.DEFAULT_CHUNK_OVERLAP, ge=0, le=600)
    embedder: str = config.DEFAULT_EMBEDDER


class JobRequest(BaseModel):
    config: dict = {}
    case_ids: list[str] | None = None


# ----------------------------------------------------------------------------------- system
@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": VERSION, "chunks": len(kb.store()), "docs": len(kb.doc_paths()), "readonly": READONLY, "protected": bool(PASSWORD)}


@app.get("/api/models")
def models(refresh: bool = False) -> dict:
    return list_models(force=refresh)


# ------------------------------------------------------------------------------------- chat
@app.post("/api/chat")
def chat(req: ChatRequest) -> dict:
    cfg = AgentConfig.from_dict(req.config)
    final, steps = run_agent_sync(req.question, req.history, cfg, kb)
    return {**final, "trace": steps}


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    """Server-sent events: one `step` event per trace step, then `final`."""
    cfg = AgentConfig.from_dict(req.config)
    q: queue.Queue = queue.Queue()

    def worker() -> None:
        try:
            for ev in run_agent(req.question, req.history, cfg, kb):
                q.put(ev)
        except Exception as exc:  # noqa: BLE001
            q.put({"type": "fatal", "error": str(exc)})
        q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    async def gen():
        loop = asyncio.get_running_loop()
        while True:
            ev = await loop.run_in_executor(None, q.get)
            if ev is None:
                break
            yield f"event: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------------------- playground
@app.post("/api/tokenize")
def tokenize_api(body: dict) -> dict:
    text = str(body.get("text", ""))[:20000]
    toks = tokenize(text)
    return {"tokens": toks, "count": sum(1 for t in toks if t.strip()), "chars": len(text), "approximate": True}


@app.post("/api/playground")
def playground(req: PlaygroundRequest) -> dict:
    try:
        llm = get_llm(req.model)
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc
    messages = ([{"role": "system", "content": req.system}] if req.system.strip() else []) + [{"role": "user", "content": req.prompt}]
    outputs = []
    for _ in range(req.n):
        try:
            r = llm.chat(messages, temperature=min(2.0, max(0.0, req.temperature)), top_p=min(1.0, max(0.05, req.top_p)), max_tokens=req.max_tokens)
        except LLMError as exc:
            raise HTTPException(502, str(exc)) from exc
        outputs.append({"text": r.text, "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens, "latency_ms": r.latency_ms})
        if req.temperature == 0:
            break
    return {"outputs": outputs, "model": req.model, "simulated": req.model.startswith("mock:")}


# -------------------------------------------------------------------------------- knowledge
@app.get("/api/knowledge")
def knowledge() -> dict:
    store = kb.store()
    return {
        "docs": kb.list_docs(),
        "index": {"chunk_size": kb.chunk_size, "overlap": kb.overlap, "embedder": kb.embedder, "chunks": len(store)},
        "avg_chunk_tokens": int(sum(count_tokens(c.text) for c in store.chunks) / max(1, len(store))),
    }


@app.post("/api/knowledge/upload")
async def upload(files: list[UploadFile] = File(...)) -> dict:
    _writable()
    saved = []
    for f in files:
        data = await f.read()
        if len(data) > 15 * 1024 * 1024:
            raise HTTPException(413, f"{f.filename} is larger than 15 MB.")
        try:
            saved.append(kb.add_upload(f.filename or "upload.txt", data))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    return {"saved": saved, **knowledge()}


@app.delete("/api/knowledge/docs/{name}")
def delete_doc(name: str) -> dict:
    _writable()
    if not kb.delete(name):
        raise HTTPException(404, "Document not found.")
    return knowledge()


@app.post("/api/knowledge/reset-demo")
def reset_demo() -> dict:
    _writable()
    for p in config.UPLOAD_DIR.glob("*"):
        p.unlink()
    demo_data.write_demo_docs(force=True)
    kb.invalidate()
    return knowledge()


@app.post("/api/knowledge/reindex")
def reindex(req: ReindexRequest) -> dict:
    _writable()
    try:
        kb.reindex(req.chunk_size, req.overlap, req.embedder)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not build the index: {exc}") from exc
    return knowledge()


@app.post("/api/knowledge/search")
def search(req: SearchRequest) -> dict:
    t0 = time.time()
    store = kb.store(req.chunk_size, None, req.embedder)
    hits = store.search(req.query, k=min(10, max(1, req.k)), mode=req.mode)
    return {"hits": [h.to_dict() for h in hits], "ms": int((time.time() - t0) * 1000), "mode": req.mode, "total_chunks": len(store)}


@app.get("/api/knowledge/chunks")
def chunks(doc: str) -> dict:
    store = kb.store()
    return {"chunks": [{"id": c.id, "heading": c.heading, "text": c.text, "tokens": count_tokens(c.text)} for c in store.chunks if c.doc == doc]}


@app.get("/api/knowledge/map")
def knowledge_map(query: str = "") -> dict:
    return kb.store().projection(query or None)


@app.get("/api/data/schema")
def data_schema() -> dict:
    return tools.schema_details()


# --------------------------------------------------------------------------------------- evals
@app.get("/api/evals/cases")
def eval_cases() -> dict:
    from .evalset import CASES, CATEGORY_LABELS

    return {"cases": [{k: c[k] for k in ("id", "category", "question")} for c in CASES], "labels": CATEGORY_LABELS}


@app.post("/api/evals/run")
def eval_run(req: JobRequest) -> dict:
    from .evalset import CASES

    total = len([c for c in CASES if not req.case_ids or c["id"] in req.case_ids])
    cfg = {**req.config, "model": "mock:offline-demo"} if READONLY else req.config   # public demo: never spend API quota on batch jobs
    job = evals.start_job("eval", total, lambda j: evals.run_eval(cfg, kb, j, req.case_ids))
    return job.public()


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = evals.JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job.")
    out = job.public()
    if job.status in ("done", "cancelled") and job.result is not None:
        out["result"] = job.result
    return out


@app.post("/api/jobs/{job_id}/cancel")
def job_cancel(job_id: str) -> dict:
    job = evals.JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job.")
    job.cancel = True
    return job.public()


@app.get("/api/evals/runs")
def eval_runs() -> dict:
    return {"runs": evals.list_runs()}


@app.get("/api/evals/runs/{run_id}")
def eval_run_get(run_id: str) -> dict:
    run = evals.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found.")
    return run


@app.delete("/api/evals/runs/{run_id}")
def eval_run_delete(run_id: str) -> dict:
    if not evals.delete_run(run_id):
        raise HTTPException(404, "Run not found.")
    return {"runs": evals.list_runs()}


# ------------------------------------------------------------------------------------ red team
@app.get("/api/redteam/attacks")
def attacks() -> dict:
    return {"attacks": [{k: a[k] for k in ("id", "name", "kind", "prompt", "explain", "success_if")} for a in redteam.ATTACKS + [redteam.CONTROL]]}


@app.post("/api/redteam/run")
def redteam_run(req: JobRequest) -> dict:
    total = (len(redteam.ATTACKS) + 1) * 2
    cfg = {**req.config, "model": "mock:offline-demo"} if READONLY else req.config
    job = evals.start_job("redteam", total, lambda j: redteam.run_redteam(cfg, kb, j))
    return job.public()


# --------------------------------------------------------------------------------------- web
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(config.WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")


@app.exception_handler(LLMError)
async def llm_error(_, exc: LLMError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": str(exc)})
