"""Answer verification for web/document answers.

Small local models read a source correctly but then mis-copy a name ("Akay" for "Akaay") or invent a detail.
This module checks every name and number in the answer against the evidence the model was given:
  * a near-miss spelling is repaired to the exact spelling in the sources;
  * a name or number that appears nowhere in the sources is reported as unverified.
Purely mechanical (difflib + regex): no second model call, so it costs no time.
"""
from __future__ import annotations

import difflib
import re

_CAP = re.compile(r"\b[A-Z][a-zA-Z]{2,}\b")
_WORD = re.compile(r"[A-Za-z]{3,}")
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_CITE = re.compile(r"\[\d+\]")
_STOP = set(
    "The This That These Those His Her Their Its According Based Answer Note Sorry However Unfortunately Yes No Here There When Where "
    "Who What Which How Why And But Also For From With Without Not Source Sources Result Results Search Web Both One Two Three "
    "January February March April May June July August September October November December Monday Tuesday Wednesday Thursday Friday "
    "Saturday Sunday".split()
)
_ABSTAIN = re.compile(r"couldn'?t find|could not find|can'?t find|cannot find|do(?:es)? not (?:contain|mention)|don'?t know|do not know|no information|not (?:found|available)", re.I)


def _sentence_start(text: str, pos: int) -> bool:
    before = text[:pos].rstrip(" *_\"'(")
    return not before or before[-1] in ".!?:\n"


def check_answer(answer: str, evidence: str, question: str = "") -> tuple[str, list[tuple[str, str]], list[str]]:
    """Returns (possibly repaired answer, [(wrong, fixed)], [unverified items])."""
    if not evidence or _ABSTAIN.search(answer):
        return answer, [], []
    ev_words = {w for w in _WORD.findall(evidence)}
    ev_lower = {w.lower(): w for w in ev_words}
    ev_lower_caps = {w.lower(): w for w in ev_words if w[0].isupper()}
    q_lower = {w.lower() for w in _WORD.findall(question)}
    ev_nums = {n.replace(",", "") for n in _NUM.findall(_CITE.sub("", evidence))} | {n.replace(",", "") for n in _NUM.findall(question)}
    repaired: list[tuple[str, str]] = []
    unverified: list[str] = []

    def fix(m: re.Match) -> str:
        word = m.group(0)
        lw = word.lower()
        if word in _STOP or lw in ev_lower or lw in q_lower:
            return word
        rest = answer[m.end():]
        if _sentence_start(answer, m.start()) and not re.match(r"\s+[A-Z]|'s\b", rest):
            return word  # ordinary sentence-opening word, not a name
        pool = list(ev_lower_caps) or list(ev_lower)
        close = difflib.get_close_matches(lw, pool, n=1, cutoff=0.8)
        if close:
            fixed = (ev_lower_caps.get(close[0]) or ev_lower[close[0]])
            repaired.append((word, fixed))
            return fixed
        unverified.append(word)
        return word

    new = _CAP.sub(fix, answer)
    for n in _NUM.findall(_CITE.sub("", new)):
        plain = n.replace(",", "")
        if (len(plain) >= 2 or "." in plain) and plain not in ev_nums and n not in unverified:
            unverified.append(n)
    return new, repaired, unverified


_LEAD_CITES = re.compile(r"^\s*((?:\[\d+\][\s,;]*)+)(.+)$", re.S)
_CITE_RUN = re.compile(r"(?:\[\d+\]\s*){2,}")


def tidy_citations(text: str, limit: int = 3) -> str:
    """Small models often dump every citation at the start ("[1] [3] [4] [5] Answer..."). Move them to the end of the
    sentence, remove duplicates and keep at most `limit` per spot."""
    def collapse(cites: list[str]) -> str:
        seen: list[str] = []
        for c in cites:
            if c not in seen:
                seen.append(c)
        return "".join(seen[:limit])

    m = _LEAD_CITES.match(text)
    if m and m.group(2).strip():
        body, cites = m.group(2).strip().lstrip(" .,;:-").strip(), collapse(re.findall(r"\[\d+\]", m.group(1)))
        if body:
            text = f"{body[:-1]} {cites}{body[-1]}" if body[-1] in ".!?" else f"{body} {cites}"
    return _CITE_RUN.sub(lambda r: collapse(re.findall(r"\[\d+\]", r.group(0))) + " ", text).replace("  ", " ").replace(" .", ".").strip()
