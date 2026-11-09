"""Release metadata stays consistent (G1, G9): one Apache-2.0 licence for every package, identical
LICENSE/NOTICE copies, and one lab version in pyproject, the package and the changelog."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYPROJECTS = [ROOT / "pyproject.toml", *sorted((ROOT / "packages").glob("*/pyproject.toml"))]


def test_every_package_declares_apache_2_and_ships_identical_licence_files():
    assert len(PYPROJECTS) == 4
    for f in PYPROJECTS:
        proj = tomllib.loads(f.read_text())["project"]
        assert proj["license"] == "Apache-2.0", f
        assert proj["license-files"] == ["LICENSE", "NOTICE"], f
        assert not any(c.startswith("License ::") for c in proj.get("classifiers", [])), f  # PEP 639
        for name in ("LICENSE", "NOTICE"):
            assert (f.parent / name).read_bytes() == (ROOT / name).read_bytes(), (f.parent, name)
    assert "Apache License" in (ROOT / "LICENSE").read_text() and "Version 2.0, January 2004" in (ROOT / "LICENSE").read_text()
    assert "research/public/" in (ROOT / "NOTICE").read_text()


def test_lab_version_is_consistent():
    import eidos
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert eidos.__version__ == version
    changelog = (ROOT / "CHANGELOG.md").read_text()
    base = re.sub(r"(rc\d+|\.dev\d+)$", "", version)
    assert f"## {base}" in changelog or f"## {version}" in changelog, "CHANGELOG has no entry for the lab version"
