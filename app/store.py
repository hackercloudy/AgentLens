"""In-memory vector store with dense, BM25 and hybrid (reciprocal-rank fusion) retrieval."""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from .chunking import Chunk, chunk_text
from .embeddings import Embedder, analyze, make_embedder


@dataclass
class Hit:
    chunk: Chunk
    score: float          # confidence-style score in [0, 1] used for the "I don't know" threshold
    dense: float
    bm25: float
    rank: int = 0

    def to_dict(self) -> dict:
        c = self.chunk
        return {
            "id": c.id,
            "doc": c.doc,
            "page": c.page,
            "heading": c.heading,
            "text": c.text,
            "score": round(self.score, 4),
            "dense": round(self.dense, 4),
            "bm25": round(self.bm25, 4),
            "rank": self.rank,
        }


@dataclass
class VectorStore:
    embedder: Embedder
    chunk_size: int
    overlap: int
    chunks: list[Chunk] = field(default_factory=list)
    matrix: np.ndarray | None = None
    _tf: list[Counter] = field(default_factory=list)
    _df: Counter = field(default_factory=Counter)
    _avgdl: float = 0.0

    # ------------------------------------------------------------------ build
    @classmethod
    def build(cls, pages: list[tuple[str, int, str]], embedder: str, chunk_size: int, overlap: int) -> "VectorStore":
        """`pages` is a list of (doc_name, page_number, text)."""
        store = cls(embedder=make_embedder(embedder), chunk_size=chunk_size, overlap=overlap)
        counters: dict[str, int] = {}
        for doc, page, text in pages:
            start = counters.get(doc, 0)
            new = chunk_text(text, doc=doc, page=page, size=chunk_size, overlap=overlap, start_idx=start)
            counters[doc] = start + len(new)
            store.chunks.extend(new)
        texts = [c.text for c in store.chunks]
        store.embedder.fit(texts)
        store.matrix = store.embedder.embed(texts) if texts else None
        store._tf = [Counter(analyze(t)) for t in texts]
        for tf in store._tf:
            for term in tf:
                store._df[term] += 1
        store._avgdl = (sum(sum(tf.values()) for tf in store._tf) / len(store._tf)) if store._tf else 0.0
        return store

    def __len__(self) -> int:
        return len(self.chunks)

    # ----------------------------------------------------------------- search
    def _bm25(self, query: str, k1: float = 1.4, b: float = 0.75) -> np.ndarray:
        q_terms = analyze(query)
        n = len(self.chunks)
        scores = np.zeros(n, dtype=np.float32)
        for term in set(q_terms):
            df = self._df.get(term, 0)
            if not df:
                continue
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for i, tf in enumerate(self._tf):
                f = tf.get(term, 0)
                if f:
                    dl = sum(tf.values())
                    scores[i] += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * dl / (self._avgdl or 1)))
        return scores

    def search(self, query: str, k: int = 4, mode: str = "hybrid") -> list[Hit]:
        if not self.chunks or self.matrix is None:
            return []
        dense = (self.matrix @ self.embedder.embed([query])[0]).astype(np.float32)
        bm25 = self._bm25(query)
        bm25_norm = bm25 / (bm25.max() or 1.0)
        # Confidence: how well the best lexical/dense signal supports the query. Used for abstention.
        if mode == "dense":
            order = np.argsort(-dense)
            conf = np.clip(dense, 0, 1)
        elif mode == "bm25":
            order = np.argsort(-bm25)
            conf = np.clip(bm25 / 12.0, 0, 1)
        else:  # hybrid: reciprocal rank fusion for ordering, blended signal for confidence
            r_dense = np.empty(len(dense), dtype=np.int32)
            r_bm25 = np.empty(len(bm25), dtype=np.int32)
            r_dense[np.argsort(-dense)] = np.arange(len(dense))
            r_bm25[np.argsort(-bm25)] = np.arange(len(bm25))
            rrf = 1 / (60 + r_dense) + 1 / (60 + r_bm25)
            order = np.argsort(-rrf)
            conf = np.clip(0.5 * np.clip(dense, 0, 1) * 2.2 + 0.5 * np.clip(bm25 / 12.0, 0, 1), 0, 1)
        hits = []
        for rank, i in enumerate(order[: max(1, k)], start=1):
            hits.append(Hit(self.chunks[i], float(conf[i]), float(dense[i]), float(bm25[i]), rank))
        return hits

    # -------------------------------------------------------------- 2-D map
    def projection(self, query: str | None = None) -> dict:
        """PCA (via SVD) of chunk embeddings to 2-D, optionally projecting a query into the same space."""
        if self.matrix is None or len(self.chunks) < 3:
            return {"points": [], "query": None}
        X = self.matrix.astype(np.float64)
        mean = X.mean(axis=0)
        _, _, vt = np.linalg.svd(X - mean, full_matrices=False)
        basis = vt[:2].T
        pts = (X - mean) @ basis
        scale = np.abs(pts).max() or 1.0
        pts = pts / scale
        out = {
            "points": [
                {"x": float(p[0]), "y": float(p[1]), "doc": c.doc, "id": c.id, "heading": c.heading, "text": c.text[:160]}
                for p, c in zip(pts, self.chunks)
            ],
            "query": None,
        }
        if query:
            q = self.embedder.embed([query])[0].astype(np.float64)
            qp = ((q - mean) @ basis) / scale
            out["query"] = {"x": float(qp[0]), "y": float(qp[1])}
        return out
