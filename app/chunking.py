"""Document chunking: paragraph/sentence aware splitting with overlap."""
from __future__ import annotations

import re
from dataclasses import dataclass

_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\[(\u0900-\u097F])")


@dataclass
class Chunk:
    doc: str
    page: int
    idx: int
    text: str
    heading: str = ""

    @property
    def id(self) -> str:
        return f"{self.doc}#{self.idx}"


def _units(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def chunk_text(
    text: str,
    doc: str,
    page: int = 1,
    size: int = 600,
    overlap: int = 80,
    start_idx: int = 0,
) -> list[Chunk]:
    """Greedy packing of paragraphs up to `size` characters, carrying `overlap` chars forward."""
    size = max(120, int(size))
    overlap = max(0, min(int(overlap), size // 2))
    chunks: list[Chunk] = []
    state = {"buf": "", "buf_heading": "", "heading": ""}

    def flush() -> None:
        body = state["buf"].strip()
        if body:
            chunks.append(
                Chunk(doc=doc, page=page, idx=start_idx + len(chunks), text=body, heading=state["buf_heading"])
            )
        tail = body[-overlap:] if overlap else ""
        if tail:
            cut = tail.find(" ")
            tail = tail[cut + 1 :] if cut != -1 else tail
        state["buf"] = tail + (" " if tail else "")
        state["buf_heading"] = state["heading"]

    for unit in _units(text):
        if unit.startswith("#"):
            state["heading"] = unit.lstrip("# ").strip()
        pieces = [unit]
        if len(unit) > size:
            pieces, cur = [], ""
            for sent in _SENT.split(unit):
                if cur and len(cur) + len(sent) + 1 > size:
                    pieces.append(cur)
                    cur = sent
                else:
                    cur = f"{cur} {sent}".strip()
            if cur:
                pieces.append(cur)
        for piece in pieces:
            if state["buf"].strip() and len(state["buf"]) + len(piece) + 2 > size:
                flush()
            if not state["buf"].strip():
                state["buf_heading"] = state["heading"]
            state["buf"] += piece + "\n\n"
    flush()
    return [c for c in chunks if len(c.text) > 20]
