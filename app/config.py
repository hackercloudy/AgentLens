"""Central configuration. Everything can be overridden with environment variables."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("AGENTLENS_DATA", ROOT / "data"))
DOCS_DIR = DATA_DIR / "docs"
UPLOAD_DIR = DATA_DIR / "uploads"
INDEX_DIR = DATA_DIR / "index"
EVAL_DIR = DATA_DIR / "evals"
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "sales.db"
WEB_DIR = Path(__file__).resolve().parent / "web"

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
# Any OpenAI-compatible endpoint works (Groq, OpenAI, vLLM, LM Studio, ...).
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

DEFAULT_CHUNK_SIZE = int(os.getenv("AGENTLENS_CHUNK_SIZE", "600"))
DEFAULT_CHUNK_OVERLAP = int(os.getenv("AGENTLENS_CHUNK_OVERLAP", "80"))
DEFAULT_EMBEDDER = os.getenv("AGENTLENS_EMBEDDER", "hash")  # "hash" or "ollama:<model>"
DEFAULT_CONTEXT_WINDOW = int(os.getenv("AGENTLENS_CONTEXT_WINDOW", "8192"))

# A planted secret used to *test* data-leak defences (see safety.py / Red Team tab).
CANARY = "CANARY-7f3a-PLUM"

for _d in (DATA_DIR, DOCS_DIR, UPLOAD_DIR, INDEX_DIR, EVAL_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)
