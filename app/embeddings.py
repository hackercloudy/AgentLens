"""Embedding backends.

* ``HashingEmbedder`` - fully offline. Signed feature hashing over word uni/bi-grams and
  character tri-grams, re-weighted with IDF learned from the indexed corpus. Lexical, fast,
  zero downloads: ideal for demos, tests and CI.
* ``OllamaEmbedder`` - real neural embeddings (e.g. ``nomic-embed-text``, ``bge-m3``) via a
  local Ollama server. Use this for semantic and cross-lingual retrieval.
"""
from __future__ import annotations

import math
import re
import zlib
from typing import Protocol

import httpx
import numpy as np

from . import config

_WORD = re.compile(r"[A-Za-z0-9\u00C0-\u024F\u0900-\u097F]+")
STOPWORDS = frozenset(
    "a an the of to in on for and or is are was were be been being by with as at from that this it its "
    "what which who whom how when where why do does did can could should would will shall may might "
    "i you he she we they them his her our their your my me us not no than then so if into out over".split()
)


def _stem(w: str) -> str:
    for suf in ("ingly", "edly", "ations", "ation", "ments", "ment", "ings", "ing", "ies", "ied", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)] + ("y" if suf in ("ies", "ied") else "")
    return w


def analyze(text: str) -> list[str]:
    """Lowercase, drop stopwords, light stemming. Shared by BM25 and the hashing embedder."""
    return [_stem(w) for w in (m.lower() for m in _WORD.findall(text)) if w not in STOPWORDS]


class Embedder(Protocol):
    name: str

    def fit(self, texts: list[str]) -> None: ...
    def embed(self, texts: list[str]) -> np.ndarray: ...


class HashingEmbedder:
    def __init__(self, dim: int = 2048):
        self.dim = dim
        self.name = "hash"
        self.idf = np.ones(dim, dtype=np.float32)

    def _features(self, text: str) -> dict[int, float]:
        toks = analyze(text)
        feats: dict[int, float] = {}

        def add(key: str, weight: float) -> None:
            h = zlib.crc32(key.encode("utf-8"))
            idx = h % self.dim
            sign = 1.0 if (h >> 31) & 1 else -1.0
            feats[idx] = feats.get(idx, 0.0) + sign * weight

        for t in toks:
            add("w:" + t, 1.0)
        for a, b in zip(toks, toks[1:]):
            add(f"b:{a}_{b}", 0.7)
        for t in toks:
            padded = f"^{t}$"
            for i in range(len(padded) - 2):
                add("c:" + padded[i : i + 3], 0.15)
        return feats

    def _vec(self, text: str, use_idf: bool = True) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        for idx, val in self._features(text).items():
            v[idx] = math.copysign(1 + math.log(abs(val)), val) if abs(val) >= 1 else val
        if use_idf:
            v *= self.idf
        n = np.linalg.norm(v)
        return v / n if n else v

    def fit(self, texts: list[str]) -> None:
        df = np.zeros(self.dim, dtype=np.float32)
        for t in texts:
            idxs = {i for i in self._features(t)}
            for i in idxs:
                df[i] += 1
        n = max(1, len(texts))
        self.idf = (np.log((n + 1) / (df + 1)) + 1).astype(np.float32)

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.vstack([self._vec(t) for t in texts])


class OllamaEmbedder:
    def __init__(self, model: str):
        self.model = model
        self.name = f"ollama:{model}"

    def fit(self, texts: list[str]) -> None:  # nothing to learn
        return None

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 1), dtype=np.float32)
        out: list[list[float]] = []
        for i in range(0, len(texts), 32):
            r = httpx.post(
                f"{config.OLLAMA_HOST}/api/embed",
                json={"model": self.model, "input": texts[i : i + 32]},
                timeout=120,
            )
            r.raise_for_status()
            out.extend(r.json()["embeddings"])
        m = np.asarray(out, dtype=np.float32)
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        return m / np.where(norms == 0, 1, norms)


def make_embedder(name: str) -> Embedder:
    if name.startswith("ollama:"):
        return OllamaEmbedder(name.split(":", 1)[1])
    return HashingEmbedder()
