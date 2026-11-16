"""Knowledge base manager: loads documents (PDF / TXT / MD), builds and caches vector stores."""
from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from . import config
from .store import VectorStore

SUPPORTED = {".pdf", ".txt", ".md"}


def read_pages(path: Path) -> list[tuple[str, int, str]]:
    """Return [(doc_name, page_number, text)] for a file."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = []
        for i, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append((path.name, i, text))
        return pages
    text = path.read_text(encoding="utf-8", errors="ignore")
    return [(path.name, 1, text)] if text.strip() else []


class KnowledgeBase:
    """Owns the document folders and a small cache of built stores keyed by index settings."""

    def __init__(self) -> None:
        self.chunk_size = config.DEFAULT_CHUNK_SIZE
        self.overlap = config.DEFAULT_CHUNK_OVERLAP
        self.embedder = config.DEFAULT_EMBEDDER
        self._cache: dict[tuple, VectorStore] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------- documents
    def doc_paths(self) -> list[Path]:
        paths = [p for d in (config.DOCS_DIR, config.UPLOAD_DIR) for p in sorted(d.glob("*")) if p.suffix.lower() in SUPPORTED]
        return paths

    def list_docs(self) -> list[dict]:
        store = self.store()
        per_doc: dict[str, int] = {}
        for c in store.chunks:
            per_doc[c.doc] = per_doc.get(c.doc, 0) + 1
        out = []
        for p in self.doc_paths():
            out.append(
                {
                    "name": p.name,
                    "kind": p.suffix.lower().lstrip("."),
                    "bytes": p.stat().st_size,
                    "chunks": per_doc.get(p.name, 0),
                    "source": "uploaded" if p.parent == config.UPLOAD_DIR else "demo",
                }
            )
        return out

    def add_upload(self, filename: str, data: bytes) -> str:
        name = Path(filename).name.replace(" ", "_")
        if Path(name).suffix.lower() not in SUPPORTED:
            raise ValueError("Only .pdf, .txt and .md files are supported.")
        (config.UPLOAD_DIR / name).write_bytes(data)
        self.invalidate()
        return name

    def delete(self, name: str) -> bool:
        for d in (config.UPLOAD_DIR, config.DOCS_DIR):
            p = d / Path(name).name
            if p.exists():
                p.unlink()
                self.invalidate()
                return True
        return False

    # ----------------------------------------------------------------- index
    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    def _fingerprint(self, extra: list[tuple[str, int, str]] | None) -> str:
        h = hashlib.sha1()
        for p in self.doc_paths():
            h.update(p.name.encode())
            h.update(str(p.stat().st_mtime_ns).encode())
            h.update(str(p.stat().st_size).encode())
        for name, page, text in extra or []:
            h.update(name.encode())
            h.update(text.encode())
        return h.hexdigest()

    def store(
        self,
        chunk_size: int | None = None,
        overlap: int | None = None,
        embedder: str | None = None,
        extra_pages: list[tuple[str, int, str]] | None = None,
    ) -> VectorStore:
        cs = int(chunk_size or self.chunk_size)
        ov = int(self.overlap if overlap is None else overlap)
        emb = embedder or self.embedder
        key = (cs, ov, emb, self._fingerprint(extra_pages))
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        pages: list[tuple[str, int, str]] = []
        for p in self.doc_paths():
            pages.extend(read_pages(p))
        pages.extend(extra_pages or [])
        built = VectorStore.build(pages, embedder=emb, chunk_size=cs, overlap=ov)
        with self._lock:
            if len(self._cache) > 12:
                self._cache.clear()
            self._cache[key] = built
        return built

    def reindex(self, chunk_size: int, overlap: int, embedder: str) -> VectorStore:
        self.chunk_size, self.overlap, self.embedder = int(chunk_size), int(overlap), embedder
        self.invalidate()
        return self.store()
