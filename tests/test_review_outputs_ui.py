import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from openpyxl import load_workbook
from pptx import Presentation
from pptx.util import Inches

from cva.review.audit import AuditLog
from cva.service import ServiceError
from tests.conftest import SYN


def review_fully(svc, rid, who="Rev"):
    for p in svc.proposals(rid):
        if p["type"] in ("override", "licensing_assumption"):
            svc.decide_proposal(rid, p["id"], "accept", who)
    svc.run_agent(rid, "narrative")
    for p in svc.proposals(rid):
        if p["status"] == "pending":
            svc.decide_proposal(rid, p["id"], "accept", who)
    for code in sorted({i.code for i in svc.result(rid).issues if i.severity in ("error", "warn")}):
        svc.ack_issue(rid, code, who, "reviewed")
    svc.ack_parity(rid, who, "ok")
    svc.ack_licensing(rid, who, "ok")
    svc.ack_estimate(rid, who, "offline estimate accepted for the test")


# ------------------------------------------------------------------------------------------------ audit
def test_audit_chain_detects_edits_and_truncation(tmp_path):
    a = AuditLog(tmp_path / "a.jsonl")
    for i in range(4):
        a.append("e", {"i": i})
    head = a.entries()[1]["hash"]
    assert a.verify(head)["ok"]
    txt = (tmp_path / "a.jsonl").read_text()
    (tmp_path / "a.jsonl").write_text(txt.replace('"i": 2', '"i": 9'))
    assert not a.verify()["ok"]
    (tmp_path / "a.jsonl").write_text("\n".join(txt.splitlines()[2:]) + "\n")      # first entries removed
    assert not a.verify(head)["ok"]


# ------------------------------------------------------------------------------------------------ lifecycle
def test_gate_blocks_finalise_until_a_human_has_dealt_with_everything(run_a):
    svc, rid = run_a
    assert not svc.gate(rid)["ready"]
    with pytest.raises(ServiceError, match="release gate is not clear"):
        svc.finalise(rid, "Rev")
    svc.run_agent(rid)
    review_fully(svc, rid)
    with pytest.raises(ServiceError):                   # confirmation is still missing
        svc.finalise(rid, "Rev")
    svc.confirm(rid, "Rev")
    assert svc.gate(rid)["ready"]
    svc.finalise(rid, "Rev", 0)
    with pytest.raises(ServiceError, match="locked"):
        svc.ack_issue(rid, "LI-GAP", "Rev", "x")
    assert svc.audit_view(rid)["verify"]["ok"]
    svc.reopen(rid, "Rev", "customer sent new data")
    assert not svc.store.state(rid)["finalised"] and not svc.gate(rid)["ready"]


def test_accepting_a_computational_proposal_invalidates_commentary_and_confirmation(run_a):
    svc, rid = run_a
    svc.run_agent(rid)
    svc.confirm(rid, "Rev")
    assert next(c for c in svc.gate(rid)["checks"] if c["id"] == "confirmed")["ok"]
    ov = next(p for p in svc.proposals(rid) if p["type"] == "override")
    svc.decide_proposal(rid, ov["id"], "accept", "Rev")
    assert not next(c for c in svc.gate(rid)["checks"] if c["id"] == "confirmed")["ok"]      # figures changed
    stale = [p for p in svc.proposals(rid) if p["type"] == "commentary"]
    assert stale and all(p["status"] == "stale" for p in stale)
    with pytest.raises(ServiceError):
        svc.decide_proposal(rid, stale[0]["id"], "accept", "Rev")


def test_decisions_need_a_name_and_cannot_repeat(run_a):
    svc, rid = run_a
    svc.run_agent(rid, "licensing")
    pid = svc.proposals(rid)[0]["id"]
    with pytest.raises(ServiceError):
        svc.decide_proposal(rid, pid, "accept", " ")
    svc.decide_proposal(rid, pid, "reject", "Rev")
    with pytest.raises(ServiceError, match="already"):
        svc.decide_proposal(rid, pid, "accept", "Rev")
    with pytest.raises(ServiceError):
        svc.ack_issue(rid, "LI-GAP", "Rev", "")


def test_edited_commentary_is_rechecked(run_a):
    svc, rid = run_a
    svc.run_agent(rid, "narrative")
    p = next(x for x in svc.proposals(rid) if x["type"] == "commentary")
    with pytest.raises(ServiceError, match="no longer matches"):
        svc.decide_proposal(rid, p["id"], "accept", "Rev", edited_text="This saves $9,999,999 a year; we recommend it.")


def test_kpis_come_from_the_log_and_state(run_a):
    svc, rid = run_a
    svc.run_agent(rid)
    review_fully(svc, rid)
    svc.confirm(rid, "Rev"); svc.log_hours(rid, 2.5, "Rev"); svc.finalise(rid, "Rev", 1)
    k = svc.kpis(rid)
    assert k["analyst_hours_logged"] == 2.5 and k["analyst_hours_vs_target"] is True
    assert k["first_pass_accepted"] is False and k["rework_rounds"] == 1 and k["llm_calls"] > 0
    assert svc.portfolio_kpis()["first_pass_pct"] == 0.0


def test_transform_package_and_import(run_a):
    svc, rid = run_a
    pkg = svc.transform_package(rid)
    assert Path(pkg["folder"], "manifest.json").exists() and pkg["status"] == "package_ready"
    r = svc.import_transform(rid, str(SYN / "aws_transform_results_estate_a_SYNTHETIC.xlsx"), "Rev")
    assert r["status"] == "imported" and len(r["per_workload"]) == 60
    assert any(x.reference == "aws_transform" for x in svc.result(rid).parity_rows)
    assert svc.overview(rid)["transform"]["servers"] == 60


# ------------------------------------------------------------------------------------------------ outputs
def test_workbook_is_internally_consistent_and_labelled_draft(run_a):
    svc, rid = run_a
    out = svc.export(rid, pdf=False)
    assert out["status"] == "DRAFT" and out["gate_open"] > 0
    wb = load_workbook(out["files"]["workbook"])
    assert {"Summary", "Workloads", "Ledger", "Exceptions", "Licensing", "Lifecycle", "Parity", "Assumptions", "Audit"} <= set(wb.sheetnames)
    sm = wb["Summary"]
    assert "DRAFT" in sm["A2"].value
    assert any(isinstance(c.value, str) and c.value.startswith("=SUM") is False and "Workloads!O" in c.value for r in sm.iter_rows() for c in r if isinstance(c.value, str))
    # arithmetic the formulas will perform, done independently in Python
    res = svc.result(rid)
    wl = wb["Workloads"]
    tot = sum(r[14].value or 0 for r in wl.iter_rows(min_row=4, max_row=3 + len(res.estate.workloads)))
    assert abs(tot - res.pricing.scenario("on_demand").total) < 1
    assert all(f.id in {c.value for c in wb["Ledger"]["A"]} for f in res.ledger.all())
    assert wb["Lifecycle"]["A1"].font.color.rgb.endswith("C00000")          # the unverified-dates warning is visible


def test_deck_is_stamped_and_has_no_leftover_tokens(run_a):
    svc, rid = run_a
    out = svc.export(rid, pdf=False, template="placeholder")
    prs = Presentation(out["files"]["deck"])
    assert len(prs.slides) == 10
    text = "\n".join(sh.text_frame.text for s in prs.slides for sh in s.shapes if sh.has_text_frame)
    assert "DRAFT" in text and "{{" not in text and "PLACEHOLDER" in text and "Figures F-" in text


def test_final_export_is_unstamped_only_after_finalise(run_a):
    svc, rid = run_a
    svc.run_agent(rid); review_fully(svc, rid); svc.confirm(rid, "Rev"); svc.finalise(rid, "Rev")
    out = svc.export(rid, pdf=False, template="placeholder")
    assert out["status"] == "FINAL" and Path(out["files"]["deck"]).name.endswith("FINAL.pptx")
    text = "\n".join(sh.text_frame.text for s in Presentation(out["files"]["deck"]).slides for sh in s.shapes if sh.has_text_frame)
    assert "DRAFT" not in text and "FINAL" in text


def test_real_template_is_filled_by_tokens_and_named_shapes(run_a, tmp_path):
    svc, rid = run_a
    t = Presentation()
    s = t.slides.add_slide(t.slide_layouts[6])
    tb = s.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1))
    tb.text_frame.text = "Customer {{customer}}: On-Demand {{F:scenario.on_demand.total}} ({{status}}) {{no.such.token}}"
    gf = s.shapes.add_textbox(Inches(1), Inches(3), Inches(6), Inches(3)); gf.name = "CHART_cost"
    bad = s.shapes.add_textbox(Inches(1), Inches(6), Inches(2), Inches(1)); bad.name = "CHART_unknown"
    p = tmp_path / "tpl.pptx"; t.save(p)
    out = svc.export(rid, template=str(p), pdf=False)
    filled = Presentation(out["files"]["deck"]).slides[0]
    text = "\n".join(sh.text_frame.text for sh in filled.shapes if sh.has_text_frame)
    assert "$" in text and "DRAFT" in text and "{{no.such.token}}" in text
    assert any(sh.has_chart for sh in filled.shapes)                           # CHART_cost became a native chart
    assert any("no.such.token" in n for n in out["notes"]) and any("CHART_unknown" in n for n in out["notes"])


# ------------------------------------------------------------------------------------------------ UI server
@pytest.fixture()
def server(svc):
    from cva.ui.server import Handler
    Handler.svc = svc
    Handler.demo_samples = True            # these tests drive the bundled samples, which only demo mode offers
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", svc
    srv.shutdown()


def call(url, body=None):
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), e.headers


def test_ui_api_flow_and_safety(server):
    base, svc = server
    code, page, h = call(base + "/api/samples")
    assert code == 200 and any(s["role"] == "estate" for s in page)
    code, r, _ = call(base + "/api/runs", {"samples": ["estate_a_retail_ola.xlsx"], "licensing_sample": "licensing_analyst_estate_a.xlsx",
                                           "notes_sample": "notes_estate_a", "collection_days": 14})
    assert code == 200
    rid = r["run_id"]
    code, ov, _ = call(f"{base}/api/run/{rid}/overview")
    assert code == 200 and ov["estate"]["workloads"] == 60 and not ov["gate"]["ready"]
    code, e, _ = call(f"{base}/api/run/{rid}/decide", {"id": "P0001", "decision": "accept", "by": "Rev"})
    assert code == 400 and "error" in e
    code, e, _ = call(f"{base}/api/run/{rid}/agent", {"task": "narrative", "mode": "stub", "by": "Rev"})
    assert code == 200 and e["model"]["real"] is False
    code, x, _ = call(base + "/api/runs", {})
    assert code == 400
    code, x, _ = call(base + "/download/run-0001/../../state.json")
    assert code == 404                                                         # no traversal
    code, x, _ = call(base + "/api/run/run-9999/overview")
    assert code == 400 and "no such run" in x["error"]
    with urllib.request.urlopen(base + "/") as resp:
        assert resp.status == 200 and "text/html" in resp.headers["Content-Type"] and resp.headers["X-Content-Type-Options"] == "nosniff"
        assert b"CVA Output Processing Agent" in resp.read()


def test_ui_page_never_uses_innerhtml_for_data():
    html = (Path(__file__).parent.parent / "cva" / "ui" / "static" / "index.html").read_text()
    assert "innerHTML" not in html and "document.write" not in html and "eval(" not in html


# ------------------------------------------------------------------------------------------------ concurrency (the UI's background job)
def test_audit_chain_survives_many_threads_with_separate_log_objects(tmp_path):
    import threading
    path = tmp_path / "audit.jsonl"

    def work(k):
        for i in range(25):
            AuditLog(path).append("e", {"thread": k, "i": i})        # a NEW object every time, as the service does

    ts = [threading.Thread(target=work, args=(k,)) for k in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    v = AuditLog(path).verify()
    assert v["ok"] and v["entries"] == 12 * 25                      # no lost entry and no forked chain


def test_a_reader_never_sees_a_half_written_state_file(svc):
    import threading
    rid = svc.create_run([str(SYN / "estate_a_retail_ola.xlsx")], collection_days=14, as_of="2026-10-20")
    stop, errors = threading.Event(), []

    def writer():
        for i in range(300):
            st = svc.store.state(rid)
            st["hours"] = [{"hours": 0.1, "by": "w", "note": "x" * 2000, "ts": str(i)}] * 5
            svc.store.save_state(rid, st)
        stop.set()

    def reader():
        while not stop.is_set():
            try:
                svc.store.state(rid)
            except Exception as e:
                errors.append(repr(e))

    ts = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors[:2]
