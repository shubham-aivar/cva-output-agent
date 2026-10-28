"""Configuration loading.

Configuration is plain TOML under ``config/`` (standard library only).  Anything an
engineer may want to change when real Ingram material arrives lives there: column
mappings, lifecycle rules, the price catalog, scenarios and thresholds.
"""
from __future__ import annotations

import json
import os
import tomllib
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .util import canonical_json, sha256_hex


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def config_dir() -> Path:
    return Path(os.environ.get("CVA_CONFIG_DIR", project_root() / "config"))


def load_toml(path: Path) -> dict[str, Any]:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        elif isinstance(v, list) and isinstance(out.get(k), list):
            out[k] = out[k] + [x for x in v if x not in out[k]]      # accepted aliases extend, never replace
        else:
            out[k] = deepcopy(v)
    return out


@dataclass
class Config:
    settings: dict[str, Any]
    scenarios: list[dict[str, Any]]
    catalog: dict[str, Any]
    lifecycle: dict[str, Any]
    root: Path
    overrides: dict[str, Any] = field(default_factory=dict)

    def section(self, name: str) -> dict[str, Any]:
        return self.settings.get(name, {})

    def digest(self) -> str:
        """Hash of everything that influences a number; stored with each run."""
        return sha256_hex(canonical_json({
            "settings": self.settings, "scenarios": self.scenarios,
            "catalog_meta": self.catalog.get("meta"), "catalog_n": len(self.catalog.get("instance", [])),
            "lifecycle_meta": self.lifecycle.get("meta"),
        }))[:16]

    def mapping(self, name: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        """Load a column mapping (``config/mappings/<name>.toml``) and merge any
        reviewer-accepted overrides on top."""
        base = load_toml(self.root / "mappings" / f"{name}.toml")
        extra = (overrides or {}).get(name) or self.overrides.get(name) or {}
        return _deep_merge(base, extra)


def load_config(overrides: dict[str, Any] | None = None, settings_patch: dict[str, Any] | None = None) -> Config:
    root = config_dir()
    settings = load_toml(root / "settings.toml")
    if settings_patch:
        settings = _deep_merge(settings, settings_patch)
    scen = load_toml(root / "scenarios.toml").get("scenario", [])
    catalog = load_toml(root / settings["pricing"]["catalog"])
    lifecycle = load_toml(root / "rules" / "lifecycle.toml")
    return Config(settings, scen, catalog, lifecycle, root, overrides or {})


def load_overrides_file(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}
