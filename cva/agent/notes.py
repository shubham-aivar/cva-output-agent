"""Stakeholder interview notes (an optional A01 input) and word-for-word quote verification.

Notes are UNTRUSTED data: they may contain text that looks like instructions.  They are only ever shown to the
model inside tool results flagged as untrusted, and a proposal may quote a note only if the quote is found
verbatim in code (``verify_quote``), so invented evidence is rejected."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..util import norm


@dataclass
class Note:
    id: str
    title: str
    text: str
    applies_to: list[str] = field(default_factory=list)


class Notes:
    def __init__(self, notes: list[Note] | None = None) -> None:
        self.notes = notes or []

    @classmethod
    def from_dir(cls, folder: str | Path | None) -> "Notes":
        if not folder or not Path(folder).exists():
            return cls()
        out: list[Note] = []
        for i, p in enumerate(sorted(Path(folder).glob("*"))):
            if p.suffix.lower() not in {".txt", ".md"}:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            applies: list[str] = []
            m = re.match(r"\s*applies_to:\s*(.+)", text.splitlines()[0] if text else "", re.I)
            if m:
                applies = [x.strip() for x in m.group(1).split(",") if x.strip()]
                text = "\n".join(text.splitlines()[1:])
            out.append(Note(f"N{i + 1}", p.name, text.strip(), applies))
        return cls(out)

    def list(self) -> list[dict]:
        return [{"id": n.id, "title": n.title, "chars": len(n.text), "applies_to": n.applies_to} for n in self.notes]

    def get(self, nid: str) -> Note | None:
        return next((n for n in self.notes if n.id == nid), None)

    def search(self, query: str, limit: int = 8) -> list[dict]:
        words = [w for w in norm(query).split() if len(w) > 2]
        hits: list[tuple[int, dict]] = []
        for n in self.notes:
            for para in re.split(r"\n\s*\n|(?<=[.!?])\s+", n.text):
                score = sum(1 for w in words if w in norm(para))
                if score:
                    hits.append((score, {"note": n.id, "title": n.title, "excerpt": para.strip()[:400]}))
        hits.sort(key=lambda x: -x[0])
        return [h[1] for h in hits[:limit]]

    def verify_quote(self, note_id: str, quote: str) -> bool:
        n = self.get(note_id)
        if not n or not quote or len(quote.strip()) < 8:
            return False
        return re.sub(r"\s+", " ", quote.strip()).lower() in re.sub(r"\s+", " ", n.text).lower()
