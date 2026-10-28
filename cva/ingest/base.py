"""Reading spreadsheets into mapped rows.

All adapters share this: load every sheet of a workbook (or a ZIP/CSV set), find the
real header row by scoring candidate rows against the configured aliases, and expose
rows by canonical field name.  A column that cannot be found is reported, never
silently skipped, so an unexpected export layout surfaces as an exception for the
analyst (and as a mapping proposal for the agent) instead of as wrong numbers.
"""
from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..model import Issue
from ..util import norm


@dataclass
class Row:
    n: int                      # 1-based spreadsheet row number
    d: dict[str, Any]           # canonical field -> raw cell value
    raw: list[Any]


@dataclass
class Table:
    file: str
    sheet: str
    header_row: int
    colmap: dict[str, int]
    headers: list[str]
    rows: list[Row]
    missing_required: list[str] = field(default_factory=list)
    unmapped_headers: list[str] = field(default_factory=list)

    def ref(self, row: Row) -> str:
        return f"{self.file}:{self.sheet}!{row.n}"


def load_sheets(path: str | Path) -> dict[str, list[list[Any]]]:
    """Return {sheet name: rows}.  Supports .xlsx/.xlsm, .zip of CSVs and a single .csv."""
    p = Path(path)
    suf = p.suffix.lower()
    if suf in {".xlsx", ".xlsm"}:
        from openpyxl import load_workbook
        wb = load_workbook(p, read_only=True, data_only=True)
        out = {ws.title: [list(r) for r in ws.iter_rows(values_only=True)] for ws in wb}
        wb.close()
        return out
    if suf == ".zip":
        out = {}
        with zipfile.ZipFile(p) as z:
            for name in z.namelist():
                if name.lower().endswith(".csv"):
                    text = z.read(name).decode("utf-8-sig", errors="replace")
                    out[Path(name).stem] = [list(r) for r in csv.reader(io.StringIO(text))]
        return out
    if suf == ".csv":
        text = p.read_bytes().decode("utf-8-sig", errors="replace")
        return {p.stem: [list(r) for r in csv.reader(io.StringIO(text))]}
    raise ValueError(f"unsupported file type: {p.name}")


def find_sheet(sheets: dict[str, list[list[Any]]], candidates: list[str]) -> str | None:
    lookup = {norm(k): k for k in sheets}
    for c in candidates:
        if norm(c) in lookup:
            return lookup[norm(c)]
    return None


def _alias_index(fields: dict[str, list[str]]) -> dict[str, str]:
    idx: dict[str, str] = {}
    for fld, aliases in fields.items():
        for a in aliases:
            idx.setdefault(norm(a), fld)
    return idx


def _is_empty_row(r: list[Any]) -> bool:
    return not any(c is not None and str(c).strip() != "" for c in r)


def read_table(file: str, sheet: str, rows: list[list[Any]], spec: dict[str, Any],
               skip_prefixes: tuple[str, ...] = ("(", "step ")) -> Table:
    """Locate the header row and map columns for ``spec`` (a mapping section)."""
    fields = {k: v for k, v in spec.items() if k != "required" and isinstance(v, list)}
    idx = _alias_index(fields)
    best: tuple[int, int, dict[str, int]] | None = None
    for i, r in enumerate(rows[:20]):
        cmap: dict[str, int] = {}
        for j, cell in enumerate(r):
            f = idx.get(norm(cell))
            if f and f not in cmap:
                cmap[f] = j
        score = len(cmap)
        if score and (best is None or score > best[0]):
            best = (score, i, cmap)
    if best is None:
        return Table(file, sheet, -1, {}, [], [], missing_required=list(spec.get("required", [])))
    _, hi, cmap = best
    headers = [("" if c is None else str(c)) for c in rows[hi]]
    mapped_cols = set(cmap.values())
    unmapped = [h for j, h in enumerate(headers) if j not in mapped_cols and h.strip()]
    out: list[Row] = []
    for i in range(hi + 1, len(rows)):
        r = rows[i]
        if _is_empty_row(r):
            continue
        first = "" if r[0] is None else str(r[0]).strip().lower()
        if first.startswith(skip_prefixes):      # template marker rows such as "(REQ'D)"
            continue
        d = {f: (r[j] if j < len(r) else None) for f, j in cmap.items()}
        out.append(Row(i + 1, d, r))
    missing = [f for f in spec.get("required", []) if f not in cmap]
    return Table(file, sheet, hi + 1, cmap, headers, out, missing, unmapped)


def best_sheet_for(sheets: dict[str, list[list[Any]]], spec: dict[str, Any]) -> str | None:
    """Pick the sheet whose header row matches the spec best (used when sheet names are unknown)."""
    fields = {k: v for k, v in spec.items() if k != "required" and isinstance(v, list)}
    idx = _alias_index(fields)
    best, best_score = None, 0
    for name, rows in sheets.items():
        for r in rows[:20]:
            score = len({idx[norm(c)] for c in r if norm(c) in idx})
            if score > best_score:
                best, best_score = name, score
    return best


def table_issues(t: Table, code_prefix: str) -> list[Issue]:
    out: list[Issue] = []
    if t.header_row < 0:
        out.append(Issue(f"{code_prefix}-NOHEADER", "error",
                         f"No recognisable header row found in sheet '{t.sheet}'. "
                         "The export layout may have changed; review the mapping.",
                         refs=[f"{t.file}:{t.sheet}"]))
        return out
    for f in t.missing_required:
        out.append(Issue(f"{code_prefix}-MISSINGCOL", "error",
                         f"Required column '{f}' was not found in sheet '{t.sheet}'. "
                         "Figures that need it cannot be computed until the mapping is fixed.",
                         refs=[f"{t.file}:{t.sheet}!{t.header_row}"], detail={"field": f}))
    return out
