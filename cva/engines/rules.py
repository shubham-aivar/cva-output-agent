"""Rule registry.  Every figure on a slide cites one of these ids and its version, so a reviewer can
see which rule produced a number and whether the rule has changed since a previous run."""
from __future__ import annotations

RULESET_VERSION = "0.1.0"

RULES: dict[str, tuple[str, str]] = {
    "RS-CPU": ("1", "Required vCPU = observed vCPU x utilisation basis / target utilisation, rounded up"),
    "RS-MEM": ("1", "Required memory = peak (or snapshot consumed) memory / target utilisation"),
    "RS-PICK": ("1", "Cheapest catalog instance meeting required vCPU and memory (burstable only when idle)"),
    "PR-INFRA": ("1", "Infrastructure hourly price x hours per year, scaled by purchase option"),
    "PR-LIC": ("1", "Windows and SQL Server license add-ons: license-included unless a finding makes BYOL eligible"),
    "PR-STORAGE": ("1", "Used storage x headroom x gp3 monthly price x 12"),
    "PR-SCEN": ("1", "Scenario total = infrastructure + licenses + storage over all workloads"),
    "LC-EOS": ("1", "End-of-support from the lifecycle rules table (all dates currently unverified)"),
    "PA-COST": ("1", "Parity: relative annual compute cost difference against a reference engine"),
    "LI-GAP": ("1", "Licensing gap: a license-bearing workload with no usable licensing finding"),
    "ES-COUNT": ("1", "Estate counts taken straight from the normalised inventory"),
    "AT-IMPORT": ("1", "Figure imported unchanged from an AWS Transform assessment result (aggregated_output.json)"),
    "AT-SIZING": ("1", "Per-server sizing imported unchanged from the AWS Transform analysis workbook; downsize/same/upsize by A01's rule from AWS's own utilization"),
}


def rv(rule: str) -> str:
    return RULES[rule][0]
