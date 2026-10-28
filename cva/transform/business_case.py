"""AWS Transform's own business case deck (``business_case_<timestamp>.pptx``): its Assessment Scope and Executive Summary slides carry AWS's official
scope figures.  Layout VERIFIED against a real deck (231-server MPA run, October 2026).  Only labelled lines are read, each by its exact label; a label that is
not found is simply absent from the result (nothing is guessed or defaulted)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_LINES = {
    "servers_in_scope": r"Total servers:\s*([\d,]+)", "windows_servers": r"Windows Servers in scope:\s*([\d,]+)", "linux_servers": r"Linux Servers in scope:\s*([\d,]+)",
    "sql_servers": r"Total SQL Servers:\s*([\d,]+)", "sql_enterprise": r"Enterprise edition:\s*([\d,]+)", "sql_standard": r"Standard edition:\s*([\d,]+)",
    "windows_desktops": r"Windows Desktops:\s*([\d,]+)",
    "server_storage_tib": r"Server Storage \(TiB\):\s*([\d,.]+)", "block_provisioned_tib": r"Block Storage Provisioned \(TiB\):\s*([\d,.]+)",
    "block_used_tib": r"Block Storage Used \(TiB\):\s*([\d,.]+)", "object_tib": r"Object Storage \(TiB\):\s*([\d,.]+)",
    "annual_spend": r"\$([\d,]+)\s+annualized spend on AWS", "region": r"Region:\s*([a-z]{2}-[a-z]+-\d)",
}


def _texts(shapes: Any) -> list[str]:
    out: list[str] = []
    for sh in shapes:
        if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
            out += [p.text for p in sh.text_frame.paragraphs]
        if getattr(sh, "has_table", False) and sh.has_table:
            out += [c.text for r in sh.table.rows for c in r.cells]
        if sh.shape_type == 6:
            out += _texts(sh.shapes)
    return out


def parse_business_case(path: str | Path) -> dict[str, Any]:
    from pptx import Presentation
    prs = Presentation(str(path))
    lines: list[str] = []
    for slide in prs.slides:
        lines += [t.strip() for t in _texts(slide.shapes) if t and t.strip()]
    text = "\n".join(lines)
    out: dict[str, Any] = {}
    for key, pat in _LINES.items():
        m = re.search(pat, text)
        if m:
            raw = m.group(1).replace(",", "")
            out[key] = float(raw) if "." in raw else (int(raw) if key != "region" else raw) if key != "region" else m.group(1)
    m = re.search(r"([\d.]+)%\s+(savings|more|higher|lower)[^\n]*compared to On-Premises", text, re.I)
    if m:
        v = float(m.group(1))
        out["vs_on_prem_pct"] = -v if m.group(2).lower() in ("more", "higher") else v
    return out
