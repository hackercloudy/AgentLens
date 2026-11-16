"""Guardrails for the agent. Each layer is small, explicit and individually testable.

Layers (all switchable from the UI so the Red Team tab can show before/after):
  1. input guard      - flags direct prompt-injection / jailbreak phrasing in the user's message
  2. context sanitiser- removes instruction-like sentences from retrieved text (indirect injection)
  3. SQL guard        - read-only, single statement, allow-listed verbs (plus a read-only DB connection)
  4. output guard     - blocks secret/canary leakage, strips external links/images, masks PII
  5. log masking      - PII never reaches the query log
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import config

# --------------------------------------------------------------------------- injection detection
_INJECTION_PATTERNS: list[tuple[str, str, int]] = [
    ("override", r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|earlier|above|all|your|any)\b[^.\n]{0,30}\b(instruction|prompt|rule|guideline|direction)s?\b", 3),
    ("reveal-secret", r"\b(reveal|print|leak|repeat|expose|tell me|give me|what is|what\'s)\b[^.\n]{0,50}\b(passphrase|password|secret|api[- ]?key|system prompt|hidden prompt)\b", 3),
    ("role-hijack", r"\b(you are now|act as|pretend to be|from now on you)\b[^.\n]{0,60}\b(dan|unfiltered|jailbroken|no restrictions|developer mode|without (any )?(rules|restrictions|filters))\b", 3),
    ("developer-mode", r"\b(developer mode|dan mode|jailbreak)\b", 2),
    ("system-notice", r"\b(system notice|important system|new instructions?|admin override)\b", 2),
    ("exfiltration", r"!\[[^\]]*\]\(https?://[^)]*\)|\bsend\b[^.\n]{0,40}\bto\b[^.\n]{0,20}https?://", 3),
]
_COMPILED = [(n, re.compile(p, re.I), s) for n, p, s in _INJECTION_PATTERNS]


@dataclass
class Finding:
    rule: str
    severity: int
    snippet: str

    def to_dict(self) -> dict:
        return {"rule": self.rule, "severity": self.severity, "snippet": self.snippet[:120]}


def detect_injection(text: str) -> list[Finding]:
    found: list[Finding] = []
    for name, rx, sev in _COMPILED:
        for m in rx.finditer(text or ""):
            found.append(Finding(name, sev, m.group(0)))
    return found


def check_input(text: str) -> tuple[bool, list[Finding]]:
    """Returns (allowed, findings). Anything with severity >= 3 is blocked."""
    findings = detect_injection(text)
    return (not any(f.severity >= 3 for f in findings)), findings


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def sanitize_context(text: str) -> tuple[str, list[Finding]]:
    """Drop whole sentences that look like instructions aimed at the AI."""
    kept: list[str] = []
    removed: list[Finding] = []
    for sent in _SENT_SPLIT.split(text):
        if not sent.strip():
            continue
        f = detect_injection(sent)
        if f:
            removed.extend(f)
            kept.append("[removed: possible prompt injection]")
        else:
            kept.append(sent)
    return " ".join(kept), removed


# ------------------------------------------------------------------------------------ SQL guard
_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|truncate|attach|detach|pragma|vacuum|reindex|grant|revoke)\b", re.I
)


def check_sql(sql: str) -> tuple[bool, str]:
    cleaned = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql or "", flags=re.S).strip().rstrip(";").strip()
    if not cleaned:
        return False, "Empty query."
    if ";" in cleaned:
        return False, "Only a single statement is allowed."
    if not re.match(r"^(select|with)\b", cleaned, re.I):
        return False, "Only SELECT queries are allowed."
    bad = _FORBIDDEN_SQL.search(cleaned)
    if bad:
        return False, f"Forbidden keyword: {bad.group(0).upper()}."
    return True, "ok"


# ---------------------------------------------------------------------------------------- PII
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)")
_AADHAAR = re.compile(r"(?<!\d)\d{4}\s\d{4}\s\d{4}(?!\d)")
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")


def _luhn(num: str) -> bool:
    digits = [int(c) for c in num if c.isdigit()]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            d -= 9 if d > 9 else 0
        total += d
    return len(digits) >= 13 and total % 10 == 0


def mask_pii(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}

    def sub(rx: re.Pattern, label: str, s: str, check=None) -> str:
        def repl(m: re.Match) -> str:
            if check and not check(m.group(0)):
                return m.group(0)
            counts[label] = counts.get(label, 0) + 1
            return f"[{label}]"

        return rx.sub(repl, s)

    text = sub(_EMAIL, "EMAIL", text)
    text = sub(_AADHAAR, "ID_NUMBER", text)
    text = sub(_PAN, "ID_NUMBER", text)
    text = sub(_CARD, "CARD", text, check=_luhn)
    text = sub(_PHONE, "PHONE", text)
    return text, counts


# ------------------------------------------------------------------------------------ output
_URL_OR_IMG = re.compile(r"!?\[[^\]]*\]\((https?://[^)]+)\)|https?://\S+")
_SECRET_PATTERNS = [re.compile(re.escape(config.CANARY), re.I), re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"), re.compile(r"\bgsk_[A-Za-z0-9]{16,}\b")]


def guard_output(text: str) -> tuple[str, list[str]]:
    """Returns (safe_text, events). Events describe what was changed so the UI can show it."""
    events: list[str] = []
    for rx in _SECRET_PATTERNS:
        if rx.search(text):
            return (
                "I can't share that. The response contained confidential information and was blocked by the output guard.",
                ["secret-leak-blocked"],
            )
    if _URL_OR_IMG.search(text):
        text = _URL_OR_IMG.sub("[external link removed]", text)
        events.append("external-link-removed")
    text, counts = mask_pii(text)
    if counts:
        events.append("pii-masked:" + ",".join(f"{k}x{v}" for k, v in counts.items()))
    return text, events
