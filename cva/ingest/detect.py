"""Recognise which export a file is, and read it with the right adapter."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import Config
from ..util import norm
from . import cloudamize_all, cloudamize_mpa, cloudamize_ola, rvtools
from .base import load_sheets
from .result import IngestResult

READERS = {
    cloudamize_ola.KIND: cloudamize_ola.read,
    cloudamize_all.KIND: cloudamize_all.read,
    cloudamize_mpa.KIND: cloudamize_mpa.read,
    rvtools.KIND: rvtools.read,
}


def detect_kind(path: str | Path) -> str | None:
    names = {norm(n) for n in load_sheets(path)}
    if {"export template", "io throughput"} <= names:
        return cloudamize_ola.KIND
    if {"compute", "license", "machine details"} <= names:
        return cloudamize_all.KIND
    if "vinfo" in names:
        return rvtools.KIND
    if "servers" in names and names & {"applications", "server to application", "application dependency"}:
        return cloudamize_mpa.KIND
    return None


def read_any(path: str | Path, cfg: Config, kind: str | None = None, overrides: dict[str, Any] | None = None,
             collection_days: int | None = None) -> IngestResult:
    k = kind or detect_kind(path)
    if k is None:
        raise ValueError(
            f"Could not recognise '{Path(path).name}'. Supported: Cloudamize OLA Accelerator export, "
            "Cloudamize 'Download All Reports' workbook, Cloudamize MPA export, RVTools export. "
            "Use the agent's intake task to profile an unfamiliar file and propose a mapping.")
    return READERS[k](path, cfg, overrides=overrides, collection_days=collection_days)


def profile_file(path: str | Path, max_rows: int = 6) -> dict[str, Any]:
    """Compact description of a file for the intake agent: sheets, sizes, first rows."""
    sheets = load_sheets(path)
    out: dict[str, Any] = {"file": Path(path).name, "detected": detect_kind(path), "sheets": {}}
    for name, rows in sheets.items():
        nonempty = [r for r in rows if any(c is not None and str(c).strip() for c in r)]
        out["sheets"][name] = {
            "rows": len(nonempty),
            "preview": [[("" if c is None else str(c)[:40]) for c in r[:14]] for r in nonempty[:max_rows]],
        }
    return out
