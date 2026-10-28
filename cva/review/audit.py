"""Hash-chained, append-only audit log (SOW 4.1: record inputs, outputs, approvals and versions).

Each entry carries the hash of the previous one, so editing or deleting an entry anywhere breaks the chain
and ``verify`` says where.  The head hash recorded at finalise also reveals a truncated tail."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from ..util import canonical_json, now_iso, sha256_hex

GENESIS = "0" * 64
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    """One lock per audit file, shared by every AuditLog object for it: the service creates a new object per call, so a lock
    held on the object would not stop two threads appending at once and forking the chain."""
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = _lock_for(path)
        path.parent.mkdir(parents=True, exist_ok=True)

    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self.path.exists():
                return []
            out = []
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    out.append(json.loads(line))
            return out

    def head(self) -> str:
        e = self.entries()
        return e[-1]["hash"] if e else GENESIS

    def append(self, event: str, data: dict[str, Any] | None = None, actor: str = "system") -> dict[str, Any]:
        with self._lock:
            ents = self.entries()
            prev = ents[-1]["hash"] if ents else GENESIS
            body = {"n": len(ents) + 1, "ts": now_iso(), "event": event, "actor": actor, "data": data or {}}
            h = sha256_hex(prev + canonical_json(body))
            entry = {**body, "prev": prev, "hash": h}
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
            return entry

    def verify(self, expected_head: str | None = None) -> dict[str, Any]:
        prev = GENESIS
        ents = self.entries()
        for e in ents:
            body = {k: e[k] for k in ("n", "ts", "event", "actor", "data")}
            if e.get("prev") != prev or sha256_hex(prev + canonical_json(body)) != e.get("hash"):
                return {"ok": False, "entries": len(ents), "broken_at": e.get("n"),
                        "message": f"chain broken at entry {e.get('n')}"}
            prev = e["hash"]
        if expected_head and expected_head not in {e["hash"] for e in ents}:
            return {"ok": False, "entries": len(ents), "broken_at": None,
                    "message": "the head recorded at finalise is no longer in the chain (entries were removed)"}
        return {"ok": True, "entries": len(ents), "broken_at": None, "message": "chain intact"}
