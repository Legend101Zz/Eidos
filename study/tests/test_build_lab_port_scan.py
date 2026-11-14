"""The lab builder must read port files of programme labs only (never sibling lab homes of sealed runs)."""

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "evals" / "build_lab.sh"


def test_port_scan_is_restricted_to_programme_labs():
    text = SCRIPT.read_text()
    globs = re.findall(r"\.glob\('([^']*)'\)", text)
    port_globs = [g for g in globs if g.endswith("ports.json")]
    assert port_globs == ["ip-*/ports.json"], port_globs
    assert "glob('*/" not in text
