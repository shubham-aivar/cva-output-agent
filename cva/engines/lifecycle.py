"""End-of-support findings from the lifecycle rules table."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..config import Config
from ..model import Estate


@dataclass
class LifecycleFinding:
    workload: str
    product: str            # os | sql
    label: str
    end_of_support: str
    status: str             # end_of_support | approaching | supported
    months: int             # negative when already past
    note: str
    source: str
    verified: bool


def _months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def evaluate(estate: Estate, cfg: Config, as_of: date) -> list[LifecycleFinding]:
    rules = cfg.lifecycle.get("rule", [])
    approaching = int(cfg.lifecycle.get("meta", {}).get("approaching_months", 18))
    idx = {(r["kind"], r["key"]): r for r in rules}
    out: list[LifecycleFinding] = []
    for w in estate.workloads:
        cands: list[tuple[str, str]] = []
        if w.os_key:
            cands.append(("os", w.os_key))
        for s in w.sql:
            if s.year:
                cands.append(("sql", s.year))
        for kind, key in cands:
            r = idx.get((kind, key))
            if not r:
                continue
            eos = date.fromisoformat(r["end_of_support"])
            m = _months_between(as_of, eos)
            status = "end_of_support" if eos <= as_of else "approaching" if m <= approaching else "supported"
            out.append(LifecycleFinding(w.name, kind, r["label"], r["end_of_support"], status, m,
                                        r.get("note", ""), r.get("source", ""), bool(r.get("verified", False))))
    return out


def summary(findings: list[LifecycleFinding]) -> dict[str, int]:
    s = {"end_of_support": 0, "approaching": 0, "supported": 0}
    for f in findings:
        s[f.status] += 1
    s["workloads_at_risk"] = len({f.workload for f in findings if f.status != "supported"})
    return s
