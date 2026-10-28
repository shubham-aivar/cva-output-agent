"""Print the SHAPE of an AWS Transform result, never customer data rows: for the analysis workbook every sheet's name, row count and column headers;
for aggregated_output.json the cost split by assessment (numbers only).  Usage:  python scripts/inspect_results.py analysis_X.xlsx [aggregated_output.json]"""
from __future__ import annotations

import json
import sys


def sheets(path: str) -> None:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        head = next((r for r in rows[:6] if any(c not in (None, "") for c in r)), ())
        n = max(0, len(rows) - (rows.index(head) + 1)) if head else 0
        print(f"SHEET {ws.title!r}: {n} row(s)")
        if ws.title.strip().lower() not in ("read me", "glossary"):
            print("   columns:", " | ".join(str(c) for c in head if c not in (None, "")))


def totals(path: str) -> None:
    d = json.load(open(path, encoding="utf-8"))
    for m in (d.get("pricing_comparison") or {}).get("models") or []:
        print(f"MODEL {m.get('pricing_model_slug')}: total {m.get('total_aws_cost')}; by_category {m.get('by_category')}")
        print("   by_category_by_assessment:", json.dumps(m.get("by_category_by_assessment"), default=str)[:1500])
    for a in d.get("assessments") or []:
        print("ASSESSMENT", a.get("assessment_type") or a.get("type") or "?", "keys:", sorted(a)[:12], "recommended_annual_cost:", (a.get("overview") or {}).get("recommended_annual_cost"))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    for p in sys.argv[1:]:
        print("==", p)
        (sheets if p.lower().endswith(".xlsx") else totals)(p)
