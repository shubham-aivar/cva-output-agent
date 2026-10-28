"""Small, dependency-free helpers shared across the package."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

_NA = {"", "-", "n/a", "na", "n.a.", "none", "null", "nan", "unknown", "--"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def norm(s: Any) -> str:
    """Lower-case, strip punctuation and collapse whitespace (header matching)."""
    if s is None:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(s).lower())).strip()


def is_blank(v: Any) -> bool:
    return v is None or str(v).strip().lower() in _NA


def to_float(v: Any) -> float | None:
    """Parse a number out of a spreadsheet cell; None when it is not numeric."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return None if v != v else float(v)  # NaN guard
    s = str(v).strip()
    if s.lower() in _NA:
        return None
    s = s.replace(",", "").replace("$", "").replace("%", "").strip()
    mult = 1.0
    if s[-1:].lower() == "k":
        mult, s = 1_000.0, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def to_bool(v: Any) -> bool | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in {"yes", "y", "true", "1"}:
        return True
    if s in {"no", "n", "false", "0"}:
        return False
    return None


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def round_money(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(float(x), nd)


# --------------------------------------------------------------------------- OS parsing
_WIN = re.compile(r"windows\s+server\s+(2000|2003|2008(?:\s*r2)?|2012(?:\s*r2)?|2016|2019|2022|2025)", re.I)
_EDITION = re.compile(r"\b(standard|datacenter|enterprise|essentials|web)\b", re.I)


def parse_os(raw: Any) -> dict[str, str | None]:
    """Normalise the many spellings of an operating system.

    Returns family (windows|linux|other|unknown), a stable ``key`` used by the
    lifecycle rules (for example ``windows_server_2012_r2`` or ``rhel_7``), a
    human ``label`` and the edition when present.
    """
    s = "" if raw is None else str(raw).strip()
    low = s.lower()
    if not s or low in _NA:
        return {"family": "unknown", "key": None, "label": None, "edition": None}
    m = _WIN.search(s)
    if m:
        ver = re.sub(r"\s+", " ", m.group(1).lower()).replace(" r2", " r2")
        key = "windows_server_" + ver.replace(" ", "_")
        ed = _EDITION.search(s)
        return {"family": "windows", "key": key,
                "label": "Windows Server " + ver.upper().replace(" R2", " R2"),
                "edition": ed.group(1).title() if ed else None}
    if "windows" in low:
        return {"family": "windows", "key": "windows_other", "label": s, "edition": None}
    m = re.search(r"red hat enterprise linux\s*(?:server\s*)?(?:release\s*)?(\d+)", s, re.I) or \
        re.search(r"\brhel\s*(\d+)", s, re.I)
    if m:
        return {"family": "linux", "key": f"rhel_{m.group(1)}", "label": f"RHEL {m.group(1)}", "edition": None}
    m = re.search(r"centos(?:\s+linux)?\s*(?:release\s*)?(\d+)", s, re.I)
    if m:
        return {"family": "linux", "key": f"centos_{m.group(1)}", "label": f"CentOS {m.group(1)}", "edition": None}
    m = re.search(r"ubuntu(?:\s+linux)?\s*(\d{2})\.(\d{2})", s, re.I)
    if m:
        return {"family": "linux", "key": f"ubuntu_{m.group(1)}_{m.group(2)}",
                "label": f"Ubuntu {m.group(1)}.{m.group(2)}", "edition": None}
    if "ubuntu" in low:
        return {"family": "linux", "key": "ubuntu_unversioned", "label": "Ubuntu (version unknown)", "edition": None}
    m = re.search(r"(?:suse linux enterprise|sles)\s*(?:server\s*)?(\d+)", s, re.I)
    if m:
        return {"family": "linux", "key": f"sles_{m.group(1)}", "label": f"SLES {m.group(1)}", "edition": None}
    if any(k in low for k in ("linux", "debian", "oracle", "amazon", "rocky", "alma", "fedora")):
        return {"family": "linux", "key": None, "label": s, "edition": None}
    return {"family": "other", "key": None, "label": s, "edition": None}


def parse_sql(edition_raw: Any, version_raw: Any) -> dict[str, str | None]:
    """Normalise SQL Server edition and marketing version from Cloudamize strings."""
    e = "" if edition_raw is None else str(edition_raw)
    v = "" if version_raw is None else str(version_raw)
    blob = f"{e} {v}"
    edition = None
    for name in ("Enterprise", "Standard", "Express", "Web", "Developer"):
        if re.search(name, blob, re.I):
            edition = name
            break
    year = None
    m = re.search(r"SQL Server\s*(20\d{2})", blob, re.I)
    if m:
        year = m.group(1)
    else:
        mm = re.search(r"\b(\d{2})\.\d", blob)
        table = {"16": "2022", "15": "2019", "14": "2017", "13": "2016", "12": "2014", "11": "2012", "10": "2008"}
        if mm and mm.group(1) in table:
            year = table[mm.group(1)]
    return {"edition": edition, "year": year}
