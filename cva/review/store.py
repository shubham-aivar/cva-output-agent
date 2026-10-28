"""Run store.  A run is a folder; decisions are state, results are always re-derived from inputs + state."""
from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any

from ..pipeline import RunInputs
from ..util import now_iso

DEFAULT_STATE: dict[str, Any] = {
    "acks": {}, "ack_codes": {}, "mapping_overrides": {}, "assumptions": [], "sizing_overrides": {},
    "proposals": [], "parity_reviewed": None, "licensing_ack": None, "confirmed": None, "finalised": None,
    "hours": [], "outcomes": {}, "commentary": {}, "flags": {}, "transform": {"mode": "off", "per_workload": {}, "status": "not_run"},
    "options": {}, "transform_assessment": None, "transform_pending": None, "estimate_ack": None, "offline_estimate": False,
    "transform_job": None, "transform_artifact_listing": {}, "transform_artifact_index": {},
}


class RunStore:
    def __init__(self, base: str | Path) -> None:
        self.base = Path(base)
        self.base.mkdir(parents=True, exist_ok=True)

    def list_runs(self) -> list[str]:
        return sorted(p.name for p in self.base.iterdir() if p.is_dir() and (p / "manifest.json").exists())

    def path(self, run_id: str) -> Path:
        p = (self.base / run_id).resolve()
        if self.base.resolve() not in p.parents:
            raise ValueError("invalid run id")
        return p

    def exists(self, run_id: str) -> bool:
        try:
            return (self.path(run_id) / "manifest.json").exists()
        except ValueError:
            return False

    def new_id(self) -> str:
        n = len(self.list_runs()) + 1
        while (self.base / f"run-{n:04d}").exists():
            n += 1
        return f"run-{n:04d}"

    def create(self, inp: RunInputs, label: str = "") -> str:
        rid = self.new_id()
        p = self.base / rid
        (p / "inputs").mkdir(parents=True)
        files = []
        for f in inp.files:
            dst = p / "inputs" / Path(f).name
            shutil.copy2(f, dst)
            files.append(dst.name)
        lic = None
        if inp.licensing_file:
            dst = p / "inputs" / Path(inp.licensing_file).name
            shutil.copy2(inp.licensing_file, dst)
            lic = dst.name
        manifest = {"run_id": rid, "created": now_iso(), "label": label, "files": files, "licensing_file": lic,
                    "kind": inp.kind, "collection_days": inp.collection_days, "as_of": inp.as_of,
                    "customer_label": inp.customer_label, "settings_patch": inp.settings_patch}
        (p / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        state = json.loads(json.dumps(DEFAULT_STATE))
        self.save_state(rid, state)
        return rid

    def manifest(self, rid: str) -> dict[str, Any]:
        return json.loads((self.path(rid) / "manifest.json").read_text(encoding="utf-8"))

    def state(self, rid: str) -> dict[str, Any]:
        f = self.path(rid) / "state.json"
        st = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        for k, v in DEFAULT_STATE.items():
            st.setdefault(k, json.loads(json.dumps(v)))
        return st

    def save_state(self, rid: str, st: dict[str, Any]) -> None:
        target = self.base / rid / "state.json"
        tmp = target.with_name(f"state.json.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(st, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, target)                      # atomic: concurrent readers never see a half-written file

    def inputs_for(self, rid: str) -> RunInputs:
        m, st, p = self.manifest(rid), self.state(rid), self.path(rid)
        tr = st["transform"].get("per_workload") or None
        return RunInputs(
            files=[str(p / "inputs" / f) for f in m["files"]],
            licensing_file=str(p / "inputs" / m["licensing_file"]) if m.get("licensing_file") else None,
            kind=m.get("kind"), collection_days=m.get("collection_days"), as_of=m.get("as_of"),
            customer_label=m.get("customer_label"), mapping_overrides=st["mapping_overrides"],
            assumptions=st["assumptions"], sizing_overrides=st["sizing_overrides"], transform_results=tr,
            settings_patch=m.get("settings_patch") or {}, aws=st.get("transform_assessment"),
            offline_estimate=bool(st.get("offline_estimate")))
