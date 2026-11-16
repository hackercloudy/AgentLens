"""Approximate tokenizer used for the token counter, context budget and the visualiser.

Real LLM tokenizers (BPE / SentencePiece) are model specific and need downloaded
vocab files. This is a transparent approximation: a GPT-style pre-tokenizer
followed by a length-based sub-word split. It is good enough to teach the idea
and to budget a context window, and it is labelled as approximate in the UI.
"""
from __future__ import annotations

import re

_PATTERN = re.compile(
    r"\s?[A-Za-z\u00C0-\u024F\u0900-\u097F]+|\s?\d+|\s?[^\sA-Za-z\d\u00C0-\u024F\u0900-\u097F]+|\s+"
)
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")


def _split_word(piece: str) -> list[str]:
    lead = ""
    word = piece
    if piece[:1].isspace():
        lead, word = piece[0], piece[1:]
    if not word:
        return [piece]
    if _DEVANAGARI.search(word):
        size = 2  # Indic scripts cost many more tokens per word in most tokenizers
    elif word.isdigit():
        size = 3
    elif word.isalpha():
        size = 7 if len(word) <= 9 else 5
    else:
        size = 4
    parts = [word[i : i + size] for i in range(0, len(word), size)]
    if lead:
        parts[0] = lead + parts[0]
    return parts


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for piece in _PATTERN.findall(text or ""):
        if piece.isspace():
            out.append(piece)
            continue
        out.extend(_split_word(piece))
    return out


def count_tokens(text: str) -> int:
    """Whitespace-only pieces are free (they are merged into neighbours by real tokenizers)."""
    return sum(1 for t in tokenize(text) if t.strip())
