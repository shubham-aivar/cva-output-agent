"""Checks model-written text against the facts.  Code decides what is true; the model only words it.

Rejects (a) any number that is not a recorded figure (within rounding), and (b) advice or commitment
language, because the SOW leaves recommendations and pricing approval to the Technical Consultant."""
from __future__ import annotations

import re
from typing import Any

BANNED = ("recommend", "we advise", "should ", "must ", "guarantee", "will save", "ensure that", "proceed with",
          "before proceeding", "best option", "ignore previous", "approved", "ready for release")
_NUM = re.compile(r"\$?\d[\d,]*(?:\.\d+)?\s*(?:%|k\b|m\b|M\b|K\b|tb\b|gb\b)?", re.I)


def _variants(v: float) -> set[float]:
    out = {float(v), round(v), round(v, 1), round(v, 2)}
    if abs(v) >= 1000:
        out |= {round(v / 1000, 1), round(v / 1000, 0), round(v / 1000, 2)}
    if abs(v) >= 1_000_000:
        out |= {round(v / 1_000_000, 2), round(v / 1_000_000, 1)}
    return out


def allowed_numbers(figures: list[Any]) -> set[float]:
    s: set[float] = set()
    for f in figures:
        if isinstance(f.value, (int, float)) and not isinstance(f.value, bool):
            s |= _variants(float(f.value))
    return s


def _to_float(tok: str) -> tuple[float, str] | None:
    t = tok.strip().lower().replace("$", "").replace(",", "")
    suffix = ""
    for suf in ("%", "k", "m", "tb", "gb"):
        if t.endswith(suf):
            suffix, t = suf, t[: -len(suf)].strip()
            break
    try:
        return float(t), suffix
    except ValueError:
        return None


def check_text(text: str, allowed: set[float]) -> list[str]:
    problems: list[str] = []
    low = text.lower()
    for b in BANNED:
        if b in low:
            problems.append(f"advice or commitment wording is not allowed here: '{b.strip()}'")
    for m in _NUM.finditer(text):
        tok = m.group(0)
        parsed = _to_float(tok)
        if parsed is None:
            continue
        val, suf = parsed
        end = text[m.end(): m.end() + 6].lower()
        if 1900 <= val <= 2100 and not suf:           # years
            continue
        if val <= 4 and not suf and (end.startswith("-year") or end.startswith(" year") or val == int(val)):
            if end.startswith(("-year", " year", " scenario", " pricing", " sheet")) or val <= 2:
                continue
        cands = {val} | ({val * 1000} if suf == "k" else set()) | ({val * 1_000_000} if suf == "m" else set())
        ok = any(abs(c - a) <= max(0.06, 0.006 * abs(a)) for c in cands for a in allowed)
        if not ok:
            problems.append(f"number '{tok.strip()}' is not a recorded figure")
    return problems
