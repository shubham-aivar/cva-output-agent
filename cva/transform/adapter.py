"""AWS Transform integration: one adapter interface, three implementations.

SOW 5.2 "isolation of external change": AWS Transform is reached only through this interface, so a change
on the AWS side touches one module.  Which implementation runs is a setting, not a code change:

* off              A01 works fully without AWS Transform (our own calculation engine always runs).
* console_fallback An operator runs the AWS Transform assessment in the console with the package we prepare,
                   then uploads the result file; we import it and compare it with our own numbers (SOW 5.5).
* mcp              The AWS Transform MCP server.  NOT IMPLEMENTED YET: see ``McpAdapter`` for exactly what has to
                   be verified first.  It refuses to run rather than guess.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..config import Config
from ..ingest.base import find_sheet, load_sheets, read_table, table_issues
from ..model import Issue
from ..util import is_blank, norm, to_float

CONSOLE_STEPS = [
    "Open the AWS Transform console and select (or create) the workspace agreed with Ingram (SOW OI-1).",
    "Create a migration assessment job.",
    "Upload the file(s) listed in the package manifest (Cloudamize MPA or other accepted export).",
    "Review the discovery summary and its data-quality warnings.",
    "Configure the scenario: Region, pricing model, tenancy, SQL Server licensing option.",
    "Run the assessment, then download the data export spreadsheet (XLSX), and the PDF/PPTX if wanted.",
    "Upload the XLSX back to this run (CLI: import-transform).  The manual step is recorded in the audit log.",
]


class TransformNotConfigured(RuntimeError):
    pass


@dataclass
class TransformResult:
    mode: str
    status: str                              # not_run | package_ready | imported | error
    per_workload: dict[str, dict[str, Any]] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["issues"] = [i.to_dict() for i in self.issues]
        return d


class TransformAdapter(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...
    def prepare_package(self, source_files: list[str], out_dir: Path) -> TransformResult: ...
    def import_results(self, path: Path) -> TransformResult: ...


class OffAdapter:
    name = "off"

    def available(self) -> tuple[bool, str]:
        return False, "AWS Transform integration is switched off; the agent's own calculation layer is used alone."

    def prepare_package(self, source_files: list[str], out_dir: Path) -> TransformResult:
        return TransformResult(self.name, "not_run", notes=[self.available()[1]])

    def import_results(self, path: Path) -> TransformResult:
        return TransformResult(self.name, "not_run", notes=["Switch the adapter to console_fallback to import results."])


class ConsoleFallbackAdapter:
    name = "console_fallback"

    def __init__(self, cfg: Config, mapping_overrides: dict[str, Any] | None = None) -> None:
        self.cfg = cfg
        self.overrides = mapping_overrides

    def available(self) -> tuple[bool, str]:
        return True, "Operator-run: package prepared here, result imported back."

    def prepare_package(self, source_files: list[str], out_dir: Path) -> TransformResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        files = []
        for f in source_files:
            dst = out_dir / Path(f).name
            shutil.copy2(f, dst)
            files.append(dst.name)
        manifest = {"purpose": "Input package for an AWS Transform migration assessment (console run)",
                    "files": files, "steps": CONSOLE_STEPS,
                    "note": "Cloudamize exports are accepted as partner-tool or MPA-format input; confirm in the "
                            "discovery summary that AWS Transform recognised the format."}
        (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (out_dir / "README.txt").write_text("\n".join(f"{i + 1}. {s}" for i, s in enumerate(CONSOLE_STEPS)) + "\n",
                                            encoding="utf-8")
        return TransformResult(self.name, "package_ready", artifacts=files + ["manifest.json", "README.txt"],
                               notes=["Package ready for the console run; nothing was sent to AWS."])

    def import_results(self, path: Path) -> TransformResult:
        m = self.cfg.mapping("aws_transform", self.overrides)
        sheets = load_sheets(path)
        name = find_sheet(sheets, m["sheets"]["servers"])
        res = TransformResult(self.name, "error")
        if name is None:
            res.issues.append(Issue("TR-NOSHEET", "error", f"No recognisable server sheet in {path.name}."))
            return res
        t = read_table(path.name, name, sheets[name], m["servers"])
        res.issues += table_issues(t, "TR")
        res.issues.append(Issue("TR-UNVERIFIED", "warn",
                                "The AWS Transform results layout is assumed (no real export seen); check the mapped "
                                "columns before relying on the comparison."))
        for row in t.rows:
            nm = row.d.get("name")
            if is_blank(nm):
                continue
            cost = to_float(row.d.get("annual_cost"))
            if cost is None and to_float(row.d.get("monthly_cost")) is not None:
                cost = to_float(row.d.get("monthly_cost")) * 12.0  # type: ignore[operator]
            lic = norm(row.d.get("license") or "")
            res.per_workload[str(nm).strip()] = {
                "instance": None if is_blank(row.d.get("instance")) else str(row.d.get("instance")).strip(),
                "annual_cost": cost, "byol_sql": "byol" in lic or "bring your own" in lic,
                "pricing_model": None if is_blank(row.d.get("pricing_model")) else str(row.d.get("pricing_model"))}
        res.status = "imported" if res.per_workload else "error"
        res.notes.append(f"{len(res.per_workload)} server recommendation(s) imported from {path.name} "
                         "(manual console step; logged).")
        return res


class McpAdapter:
    """AWS Transform MCP server (awslabs).  NOT IMPLEMENTED: three things must be verified first, because the
    SOW assumes them and no evidence has been seen:

    1. That the server's ``create_job`` can create the NEW migration-assessment job (PPTX/XLSX outputs); its
       published docs describe mainframe, VMware, .NET and custom-code jobs.
    2. That a non-interactive identity can authenticate (most tools need a browser login; SSO tokens expire).
    3. The artifact tool names/arguments for uploading the Cloudamize file and downloading the XLSX export.

    Until then this adapter reports itself unavailable and the console fallback is the supported path.
    """
    name = "mcp"

    def available(self) -> tuple[bool, str]:
        try:
            import mcp  # type: ignore  # noqa: F401
        except ImportError:
            return False, 'the "mcp" package is not installed: pip install "mcp>=1.23,<2"'
        return True, "use Service.transform_run / `python -m cva transform-run RUN` (driver in cva/transform/driver.py)"

    def prepare_package(self, source_files: list[str], out_dir: Path) -> TransformResult:
        raise TransformNotConfigured(self.available()[1])

    def import_results(self, path: Path) -> TransformResult:
        raise TransformNotConfigured(self.available()[1])


def get_adapter(kind: str, cfg: Config, mapping_overrides: dict[str, Any] | None = None) -> TransformAdapter:
    kind = (kind or "off").lower()
    if kind == "console_fallback":
        return ConsoleFallbackAdapter(cfg, mapping_overrides)
    if kind == "mcp":
        return McpAdapter()
    return OffAdapter()
