"""Configuration guard.  The service's proposed scenario is compared with what we asked for, and A01 confirms only
on a match.  This exists because a re-run proved able to drop settings, add others (sustainability, network cost,
instance exclusions) and still announce that it "matches your original run exactly"."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class GuardResult:
    ok: bool
    missing: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    accepted_defaults: list[str] = field(default_factory=list)

    def report(self) -> str:
        if self.ok:
            return "proposal matches the requested configuration" + (
                f" (service defaults noted: {', '.join(self.accepted_defaults)})" if self.accepted_defaults else "")
        parts = []
        if self.missing:
            parts.append("missing from the proposal: " + ", ".join(self.missing))
        if self.forbidden:
            parts.append("present but not wanted: " + ", ".join(self.forbidden))
        return "; ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "missing": self.missing, "forbidden": self.forbidden, "accepted_defaults": self.accepted_defaults}


def check(proposal_text: str, expected: dict[str, Any]) -> GuardResult:
    low = (proposal_text or "").lower()
    missing = []
    for item in expected.get("must_contain", []):
        pat, label = (item["pattern"], item.get("label", item["pattern"])) if isinstance(item, dict) else (item, item)
        try:
            found = re.search(pat, low, re.I)
        except re.error:
            found = pat.lower() in low
        if not found:
            missing.append(label)
    forbidden = []
    for item in expected.get("forbidden", []):
        pat, label = (item["pattern"], item.get("label", item["pattern"])) if isinstance(item, dict) else (item, item)
        if re.search(pat, low, re.I):
            forbidden.append(label)
    accepted = []
    for item in expected.get("accepted_defaults", []):
        pat, label = (item["pattern"], item.get("label", item["pattern"])) if isinstance(item, dict) else (item, item)
        if re.search(pat, low, re.I):
            accepted.append(label)
    return GuardResult(not missing and not forbidden, missing, forbidden, accepted)


def settings_text(settings: dict[str, Any]) -> str:
    """The configuration, spelled out in full, sent with every start and every re-run."""
    return (f"Target region {settings['region']}. Pricing: {settings['pricing']}. Tenancy: {settings['tenancy']}. "
            f"Instance selection: {settings['instance_selection']}. Licensing: {settings['licensing']}. {settings.get('extra', '')}").strip()
