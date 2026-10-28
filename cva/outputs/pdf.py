"""PDF copy of the deck via LibreOffice (SOW A01: "PPTX with PDF copy").  Optional: if LibreOffice is absent the
run says so and the PPTX is still produced."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path


def convert_to_pdf(pptx: str | Path, outdir: str | Path | None = None) -> tuple[str | None, str]:
    pptx = Path(pptx)
    outdir = Path(outdir or pptx.parent)
    cmd = shlex.split(os.environ.get("CVA_SOFFICE", "")) or \
        ([shutil.which("soffice") or shutil.which("libreoffice")] if (shutil.which("soffice") or shutil.which("libreoffice")) else [])
    if not cmd:
        return None, "LibreOffice not found (install it, or set CVA_SOFFICE); the PPTX was produced without a PDF copy"
    try:
        subprocess.run([*cmd, "--headless", "--convert-to", "pdf", "--outdir", str(outdir), str(pptx)],
                       capture_output=True, timeout=240, check=True)
    except Exception as e:
        return None, f"PDF conversion failed: {type(e).__name__}: {e}"
    pdf = outdir / (pptx.stem + ".pdf")
    return (str(pdf), "ok") if pdf.exists() else (None, "conversion produced no file")
