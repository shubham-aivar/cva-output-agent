"""What an adapter hands back to the pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..model import Estate, Issue, LicensingFinding


@dataclass
class IngestResult:
    estate: Estate
    issues: list[Issue] = field(default_factory=list)
    licensing_hints: list[LicensingFinding] = field(default_factory=list)
    profile: dict[str, Any] = field(default_factory=dict)
    unmapped: dict[str, list[str]] = field(default_factory=dict)   # sheet -> headers nobody mapped
    sections: dict[str, str] = field(default_factory=dict)         # sheet -> mapping section name

    def merge(self, other: "IngestResult") -> None:
        self.issues += other.issues
        self.licensing_hints += other.licensing_hints
        self.unmapped.update(other.unmapped)
        self.sections.update(other.sections)
