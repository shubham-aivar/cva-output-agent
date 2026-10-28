"""Command line.  ``python -m cva <command> --help`` for details.  Typical session:

    python -m cva demo                      # synthetic estate, agent pass, what to do next
    python -m cva ui                        # the demo screen at http://127.0.0.1:8765
    python -m cva run FILE... [--licensing F] [--notes DIR]
    python -m cva agent RUN                 # propose (stub model offline; --model bedrock for the real one)
    python -m cva proposals RUN ; python -m cva decide RUN P0001 accept --by "Name"
    python -m cva ack RUN CODE --by "Name" --note "why"
    python -m cva confirm RUN --by "Name" ; python -m cva finalise RUN --by "Name"
    python -m cva export RUN [--template ingram.pptx]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config import project_root
from .service import Service, ServiceError


def _p(o) -> None:
    print(json.dumps(o, indent=2, default=str))


def _money(v) -> str:
    return "not computable" if v is None else f"${v:,.0f}"


def _summary(svc: Service, rid: str) -> None:
    ov = svc.overview(rid)
    print(f"\nRun {rid}: {ov['estate']['workloads']} workloads, data grade {ov['meta']['richness']['grade']} "
          f"({ov['meta']['richness']['label']})")
    for s in ov["scenarios"]:
        extra = f"  [{s['status']}]" if s["status"] != "computed" else ""
        print(f"  {s['label']:<22} {_money(s['total']):>14} per year{extra}  [{'AWS Transform' if s['source'] == 'aws_transform' else 'A01 estimate'}]")
    if not ov["scenarios"]:
        print("  No cost figures yet: import or run an AWS Transform assessment, or run A01's offline fallback: python -m cva estimate " + rid + " --by NAME")
    lc = ov["licensing"]
    sql = "no SQL Server detected" if lc["sql_pct"] is None else f"SQL coverage {lc['sql_pct']}%"
    print(f"  Licensing input: {lc['mode']} ({sql}, Windows {lc['windows_pct']}%)")
    print(f"  Exceptions: {ov['exceptions']['error']} error, {ov['exceptions']['warn']} warn, {ov['exceptions']['info']} info"
          f"  | parity outliers: {ov['parity'].get('outliers', 0)}")
    print(f"  Release gate: {'CLEAR' if ov['gate']['ready'] else str(ov['gate']['open']) + ' item(s) open'}")
    print(f"  Figures source: {'AWS Transform assessment' if ov['meta']['figures_source'] == 'aws_transform' else 'A01 offline estimate'}")
    if ov["meta"]["catalog"]["placeholder"] and ov["meta"]["figures_source"] != "aws_transform":
        print("  NOTE: prices come from a PLACEHOLDER catalog; lifecycle dates are unverified.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cva", description="A01 CVA Output Processing Agent")
    ap.add_argument("--runs", default=os.environ.get("CVA_RUNS_DIR", "runs"), help="folder holding runs (default ./runs, or CVA_RUNS_DIR)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, help_, **kw):
        sp = sub.add_parser(name, help=help_)
        return sp

    d = add("demo", "run a narrated demo on a synthetic estate")
    d.add_argument("--full", action="store_true", help="also act as a demo reviewer through to a final export")
    d.add_argument("--model", default="stub", choices=["stub", "bedrock"])
    r = add("run", "create a run from Cloudamize/RVTools file(s)")
    r.add_argument("files", nargs="+")
    r.add_argument("--licensing"); r.add_argument("--notes"); r.add_argument("--label", default="")
    r.add_argument("--collection-days", type=int); r.add_argument("--as-of"); r.add_argument("--customer")
    sub.add_parser("list", help="list runs")
    for n, h in (("status", "summary of a run"), ("gate", "release gate"), ("kpis", "KPIs"), ("proposals", "agent proposals"),
                 ("audit", "audit log and chain check"), ("exceptions", "data-quality exceptions"),
                 ("transform-package", "prepare the AWS Transform console package")):
        add(n, h).add_argument("run")
    a = add("agent", "run the agent (all needed tasks, or one with --task)")
    a.add_argument("run"); a.add_argument("--task"); a.add_argument("--model", default="stub", choices=["stub", "bedrock"])
    dc = add("decide", "accept or reject a proposal")
    dc.add_argument("run"); dc.add_argument("proposal"); dc.add_argument("decision", choices=["accept", "reject"])
    dc.add_argument("--by", required=True); dc.add_argument("--note", default="")
    k = add("ack", "acknowledge an exception code (or one workload with --workload)")
    k.add_argument("run"); k.add_argument("code"); k.add_argument("--by", required=True); k.add_argument("--note", required=True)
    k.add_argument("--workload")
    for n in ("ack-parity", "ack-licensing", "ack-estimate"):
        x = add(n, "acknowledge the " + n.split("-")[1] + " position"); x.add_argument("run")
        x.add_argument("--by", required=True); x.add_argument("--note", required=True)
    for n in ("confirm",):
        x = add(n, "confirm the figures"); x.add_argument("run"); x.add_argument("--by", required=True)
    f = add("finalise", "finalise (lock) the run; refused while the gate is open")
    f.add_argument("run"); f.add_argument("--by", required=True); f.add_argument("--rework-rounds", type=int, default=0)
    ro = add("reopen", "reopen a finalised run"); ro.add_argument("run"); ro.add_argument("--by", required=True); ro.add_argument("--reason", required=True)
    h = add("log-hours", "log analyst hours"); h.add_argument("run"); h.add_argument("hours", type=float); h.add_argument("--by", required=True); h.add_argument("--note", default="")
    add("transform-preflight", "10-second test of the AWS Transform connection; changes nothing in AWS")
    es = add("estimate", "run A01's own offline sizing and pricing (fallback and cross-check; placeholder prices)")
    es.add_argument("run"); es.add_argument("--by", required=True)
    ir = add("import-results", "import a real AWS Transform assessment (folder, the assessment zip or aggregated_output.json)")
    ir.add_argument("run"); ir.add_argument("path"); ir.add_argument("--by", default="cli")
    tr = add("transform-run", "A01 runs the AWS Transform assessment itself (needs AWS credentials and the MCP server)")
    tr.add_argument("run"); tr.add_argument("--enrich", action="store_true", help="second pass: fill the enrichment template with our measurements")
    tr.add_argument("--accept-proposal", action="store_true", help="a PERSON overrides a failed configuration check")
    tr.add_argument("--resume", help="WORKSPACE_ID:JOB_ID of a job already started"); tr.add_argument("--allow-unlisted-format", action="store_true")
    tr.add_argument("--auto-approve", action="store_true", help="do not stop for approval when the configuration matches (default: ask)")
    tr.add_argument("--approve", action="store_true", help="with --resume: you approve; A01 sends the go-ahead to AWS Transform")
    tr.add_argument("--confirmed-externally", action="store_true", help="with --resume: you already confirmed the scenario in the AWS Transform web application; just wait for the outputs")
    it = add("import-transform", "import AWS Transform per-server results (console-fallback, assumed layout)")
    it.add_argument("run"); it.add_argument("file"); it.add_argument("--by", required=True)
    e = add("export", "write the PPTX, PDF, XLSX and exceptions CSV")
    e.add_argument("run"); e.add_argument("--template"); e.add_argument("--no-pdf", action="store_true")
    pu = add("purge", "delete a run and its data"); pu.add_argument("run"); pu.add_argument("--confirm", action="store_true")
    u = add("ui", "start the local UI (your own files only; nothing is pre-loaded)"); u.add_argument("--port", type=int, default=8765)
    u.add_argument("--host", default=os.environ.get("CVA_HOST", "127.0.0.1"), help="address to listen on (default 127.0.0.1; anything else requires sign-in: CVA_AUTH_FILE)")
    u.add_argument("--demo-samples", action="store_true", help="also offer the bundled synthetic and public sample files (demos and tests only)")
    args = ap.parse_args(argv)
    svc = Service(args.runs)
    try:
        return _dispatch(args, svc)
    except ServiceError as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 2


def _dispatch(a, svc: Service) -> int:
    c = a.cmd
    if c == "demo":
        S = project_root() / "samples" / "synthetic"
        if not (S / "estate_a_retail_ola.xlsx").exists():
            print("generating synthetic samples...")
            import subprocess
            subprocess.run([sys.executable, str(project_root() / "scripts" / "make_samples.py")], check=True)
        print("1. Create a run from a SYNTHETIC Cloudamize OLA export with a partial licensing sheet and interview notes.")
        rid = svc.create_run([str(S / "estate_a_retail_ola.xlsx")], licensing=str(S / "licensing_analyst_estate_a.xlsx"),
                             notes_dir=str(S / "notes_estate_a"), label="demo estate A", collection_days=14)
        svc.run_offline_estimate(rid, "demo")
        print("   (demo) No AWS Transform assessment here, so A01's offline estimate was run as the fallback.")
        _summary(svc, rid)
        print("\n2. The agent decides which specialist tasks this run needs, reads the results with tools and proposes.")
        out = svc.run_agent(rid, mode=a.model)
        for t in out["tasks"]:
            print(f"   {t['task']:<10} {t['stop']:<10} {t['tool_calls']:>2} tool calls, {t['proposals_created']} proposal(s)")
        print(f"   model: {out['model']['name']}" + ("" if out["model"]["real"] else "  (offline stub: not a real agent)"))
        for p in svc.proposals(rid):
            if p["type"] in ("licensing_assumption", "licensing_request", "override"):
                print(f"   {p['id']} {p['type']}: {p['rationale'][:130]}")
        if a.full:
            _demo_review(svc, rid)
        else:
            print(f"\n3. A person reviews.  Next:  python -m cva proposals {rid}   |   python -m cva ui")
        return 0
    if c == "run":
        rid = svc.create_run(a.files, a.licensing, a.notes, a.label, a.collection_days, a.as_of, a.customer)
        print("created", rid)
        _summary(svc, rid)
    elif c == "list":
        for r in svc.list_runs():
            print(r["run_id"], r["created"], "FINAL" if r["finalised"] else "open", ", ".join(r["files"]), r["label"])
    elif c == "status":
        _summary(svc, a.run)
    elif c == "gate":
        g = svc.gate(a.run)
        for k in g["checks"]:
            print(("[x] " if k["ok"] else "[ ] ") + k["title"] + "  -  " + k["detail"])
    elif c == "kpis":
        _p(svc.kpis(a.run))
    elif c == "proposals":
        for p in svc.proposals(a.run):
            print(f"{p['id']} [{p['status']}] {p['type']} ({p['source']}): {p['rationale'][:150]}")
    elif c == "audit":
        v = svc.audit_view(a.run)
        print(v["verify"]["message"], f"({v['verify']['entries']} entries)")
        for e in v["entries"][-15:]:
            print(e["n"], e["ts"], e["event"], e["actor"])
    elif c == "exceptions":
        for i in svc.issues(a.run):
            print(f"{i['id']} {i['severity']:<5} {i['code']:<18} {i['workload'] or '-':<14} {'ack ' if i['acked'] else '    '}{i['message'][:110]}")
    elif c == "transform-package":
        _p(svc.transform_package(a.run))
    elif c == "agent":
        _p(svc.run_agent(a.run, a.task, a.model))
    elif c == "decide":
        _p(svc.decide_proposal(a.run, a.proposal, a.decision, a.by, a.note))
    elif c == "ack":
        svc.ack_issue(a.run, a.code, a.by, a.note, a.workload); print("recorded")
    elif c == "ack-parity":
        svc.ack_parity(a.run, a.by, a.note); print("recorded")
    elif c == "ack-estimate":
        svc.ack_estimate(a.run, a.by, a.note); print("recorded")
    elif c == "ack-licensing":
        svc.ack_licensing(a.run, a.by, a.note); print("recorded")
    elif c == "confirm":
        _p(svc.confirm(a.run, a.by))
    elif c == "finalise":
        _p(svc.finalise(a.run, a.by, a.rework_rounds))
    elif c == "reopen":
        svc.reopen(a.run, a.by, a.reason); print("reopened")
    elif c == "log-hours":
        svc.log_hours(a.run, a.hours, a.by, a.note); print("logged")
    elif c == "transform-preflight":
        r = svc.transform_preflight()
        for k in r["checks"]:
            print(("PASS " if k["ok"] else "FAIL ") + k["name"] + "  " + k["detail"])
        print("READY" if r["ok"] else "NOT READY: fix the FAIL lines before running an assessment")
    elif c == "estimate":
        _p(svc.run_offline_estimate(a.run, a.by))
    elif c == "transform-run":
        resume = tuple(a.resume.split(":", 1)) if a.resume else None
        _p(svc.transform_run(a.run, a.enrich, a.accept_proposal, resume, a.allow_unlisted_format,  # type: ignore[arg-type]
                             by="cli", confirmed_externally=a.confirmed_externally, require_approval=not a.auto_approve, approve_now=a.approve))
    elif c == "import-results":
        _p(svc.import_transform_results(a.run, a.path, a.by))
    elif c == "import-transform":
        r = svc.import_transform(a.run, a.file, a.by); print(r["status"], len(r["per_workload"]), "servers imported")
    elif c == "export":
        _p(svc.export(a.run, a.template, not a.no_pdf))
    elif c == "purge":
        svc.purge(a.run, a.confirm); print("purged")
    elif c == "ui":
        from .ui.server import serve
        serve(svc, a.port, a.demo_samples, a.host)
    return 0


def _demo_review(svc: Service, rid: str) -> None:
    """Acts as 'Demo Reviewer' so a whole run can be shown start to finish.  A real review is a person's job."""
    who = "Demo Reviewer"
    print("\n3. (--full) Acting as a demo reviewer: computational proposals first, then fresh commentary.")
    for p in svc.proposals(rid):
        if p["type"] in ("override", "licensing_assumption", "mapping"):
            svc.decide_proposal(rid, p["id"], "accept", who, "demo")
    svc.run_agent(rid, "narrative")
    for p in svc.proposals(rid):
        if p["status"] == "pending":
            svc.decide_proposal(rid, p["id"], "accept", who, "demo")
    res = svc.result(rid)
    for code in sorted({i.code for i in res.issues if i.severity in ("error", "warn")}):
        svc.ack_issue(rid, code, who, "reviewed in demo")
    svc.ack_parity(rid, who, "demo review")
    svc.ack_licensing(rid, who, "demo review: Windows BYOL evidence outstanding")
    svc.ack_estimate(rid, who, "demo: no AWS Transform assessment imported, offline estimate accepted")
    svc.confirm(rid, who)
    svc.finalise(rid, who, 0)
    out = svc.export(rid)
    print("4. Exported:", {k: Path(v).name for k, v in out["files"].items()}, out["notes"] or "")
    print("   Audit:", svc.audit_view(rid)["verify"]["message"])


if __name__ == "__main__":
    sys.exit(main())
