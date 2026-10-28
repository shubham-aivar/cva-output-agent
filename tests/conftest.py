import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIX = ROOT / "tests" / "fixtures"
SYN = ROOT / "samples" / "synthetic"


@pytest.fixture(scope="session", autouse=True)
def samples():
    if not (SYN / "estate_a_retail_ola.xlsx").exists():
        import subprocess
        subprocess.run([sys.executable, str(ROOT / "scripts" / "make_samples.py")], check=True)


@pytest.fixture()
def cfg():
    from cva.config import load_config
    return load_config()


@pytest.fixture()
def svc(tmp_path):
    from cva.service import Service
    return Service(tmp_path / "runs")


@pytest.fixture()
def run_a(svc):
    rid = svc.create_run([str(SYN / "estate_a_retail_ola.xlsx")], licensing=str(SYN / "licensing_analyst_estate_a.xlsx"),
                         notes_dir=str(SYN / "notes_estate_a"), collection_days=14, as_of="2026-10-20")
    svc.run_offline_estimate(rid, "test")             # opt-in: A01's own sizing and pricing
    return svc, rid


def fresh_state():
    from cva.review.store import DEFAULT_STATE
    return json.loads(json.dumps(DEFAULT_STATE))


@pytest.fixture(autouse=True)
def _no_real_mcp_server_log(monkeypatch, tmp_path):
    """Tests never read the real ~/.aws-transform-mcp/server.log of whoever runs them."""
    monkeypatch.setenv("CVA_MCP_SERVER_LOG", str(tmp_path / "no-such-server.log"))


@pytest.fixture(autouse=True)
def _no_real_login_check(monkeypatch):
    """Tests never run the AWS CLI of whoever runs them (individual tests switch it on with a stand-in)."""
    monkeypatch.setenv("CVA_LOGIN_CHECK", "off")
