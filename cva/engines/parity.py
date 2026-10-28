"""Parity: compare our independent sizing with a reference engine (Cloudamize's own recommendation, and
AWS Transform results when supplied).  SOW risk R-5: "AWS Transform estimates differ from the current
Ingram OLA method".  Differences are never hidden and never silently adopted: outliers go to the reviewer
(and to the agent's parity task, which may propose an override with a reason)."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ..config import Config
from ..model import Estate
from .pricebook import PriceBook
from .rightsize import SizingResult


@dataclass
class ParityRow:
    workload: str
    reference: str              # cloudamize | aws_transform
    ours_instance: str | None
    ref_instance: str | None
    ours_cost: float | None
    ref_cost: float | None
    diff_pct: float | None
    status: str                 # same | within_tolerance | ours_cheaper | ours_costlier | not_comparable
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _compute_cost(book: PriceBook, instance: str, os_family: str, sql_editions: list[str | None],
                  byo_sql: bool) -> float | None:
    inst = book.get(instance)
    if inst is None:
        return None
    hr = inst.linux_hr
    if os_family == "windows":
        hr += book.windows_license_hr(inst)
    if not byo_sql:
        for ed in sql_editions:
            hr += book.sql_license_hr(inst, ed)
    return hr * book.hours


def compare(estate: Estate, sizing: dict[str, SizingResult], cfg: Config, book: PriceBook,
            transform: dict[str, dict[str, Any]] | None = None) -> tuple[list[ParityRow], dict[str, Any]]:
    tol = float(cfg.section("parity").get("cost_tolerance_pct", 15))
    rows: list[ParityRow] = []
    book_fidelity: list[float] = []
    for w in estate.workloads:
        sz = sizing.get(w.name)
        eds = [s.edition for s in w.sql]
        refs: list[tuple[str, str | None, float | None, str, bool]] = []
        if w.source_instance:
            byo = "byo" in (w.source_pricing_plan or "").lower()
            cost = _compute_cost(book, w.source_instance, w.os_family, eds, byo)
            note = "" if cost is not None else "source instance not in the price book"
            if w.source_annual_cost is not None and cost:
                book_fidelity.append(100.0 * (cost - w.source_annual_cost) / w.source_annual_cost)
            refs.append(("cloudamize", w.source_instance, cost, note, byo))
        t = (transform or {}).get(w.name)
        if t:
            refs.append(("aws_transform", t.get("instance"), t.get("annual_cost"),
                         "imported AWS Transform result", bool(t.get("byol_sql", False))))
        for name, inst, rcost, note, ref_byo in refs:
            # like for like: price ours with the same SQL license treatment as the reference
            ours = (_compute_cost(book, sz.instance, w.os_family, eds, ref_byo)
                    if (sz and sz.ok and sz.instance) else None)
            if ours is None or rcost is None or rcost <= 0:
                rows.append(ParityRow(w.name, name, sz.instance if sz else None, inst, ours, rcost, None,
                                      "not_comparable", note or "cost not available"))
                continue
            diff = 100.0 * (ours - rcost) / rcost
            if sz and sz.instance == inst:
                status = "same"
            elif abs(diff) <= tol:
                status = "within_tolerance"
            else:
                status = "ours_cheaper" if diff < 0 else "ours_costlier"
            rows.append(ParityRow(w.name, name, sz.instance if sz else None, inst, round(ours, 2),
                                  round(rcost, 2), round(diff, 1), status, note))
    summ: dict[str, Any] = {"tolerance_pct": tol, "compared": sum(1 for r in rows if r.status != "not_comparable"),
                            "outliers": sum(1 for r in rows if r.status in ("ours_cheaper", "ours_costlier")),
                            "by_status": {}}
    for r in rows:
        summ["by_status"][r.status] = summ["by_status"].get(r.status, 0) + 1
    if book_fidelity:
        summ["price_book_vs_source_cost_pct_median"] = round(sorted(book_fidelity)[len(book_fidelity) // 2], 1)
        summ["price_book_vs_source_cost_pct_maxabs"] = round(max(abs(x) for x in book_fidelity), 1)
    return rows, summ


def total_compare(res_aws, pricing, estate) -> dict[str, float | int | None]:
    """Aggregate comparison (AWS Transform's per-server results come from its analysis workbook: see transform/analysis.py): compute cost per year
    from AWS Transform (On-Demand), from A01's own estimate, and from Cloudamize's own recommendation."""
    aws = res_aws.model("on_demand")
    aws_c = aws.by_category.get("compute") if aws else None
    od = pricing.scenario("on_demand")
    a01_c = (od.infra + od.windows + od.sql) if od else None
    src = [w.source_annual_cost for w in estate.workloads if w.source_annual_cost]
    cl_c = sum(src) if src else None

    def pct(a, b):
        return round(100.0 * (a - b) / b, 1) if (a is not None and b) else None

    return {"aws_compute": aws_c, "a01_compute": a01_c, "cloudamize_compute": cl_c, "cloudamize_servers": len(src),
            "aws_vs_a01_pct": pct(aws_c, a01_c), "aws_vs_cloudamize_pct": pct(aws_c, cl_c)}
