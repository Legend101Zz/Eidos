"""G5 live run preparation: the built-in eidos.pytorch.* manifests carry no `summary` or `config_schema`;
their config is documented only in the method card, so the investigator's catalog showed "summary": ""
and no way to configure them. A release without a manifest summary takes the card's Question and
Config paragraphs."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.pg

CARD = ("# demo.edit 1.0.0\n\n**Question.** What happens if a named weight is changed directly?\n\n"
        "**Config.** `edits`: list of `{entry, index?, op: set|scale|add, value}`.\n\n**Receipt.** long text that is not shown.\n")


def test_catalog_summary_falls_back_to_the_method_card(lab):
    from test_command_path import _publish, project
    pid = project(lab)
    lab.cmd(lab.admin, "register_environment", {"name": "lab", "python_executable": "/usr/bin/python3",
                                                "project_root": "/tmp", "python_version": "3.12", "installed": {}}, project=pid)
    base = {"release": "1.0.0", "kind": "artifact-op", "entry": "m:run", "scientific_effects": ["observe"]}
    _publish(lab, pid, {**base, "id": "demo.edit", "implements": "demo-edit/1", "card": "method-card.md"},
             {"m.py": "x=1\n", "method-card.md": CARD})
    _publish(lab, pid, {**base, "id": "demo.own", "implements": "demo-own/1", "summary": "its own summary",
                        "card": "method-card.md"}, {"m.py": "x=1\n", "method-card.md": CARD})
    _publish(lab, pid, {**base, "id": "demo.none", "implements": "demo-none/1"}, {"m.py": "x=1\n"})
    rows = {r["ref"]: r for r in lab.q(lab.admin, "search_components", {"limit": 50}, project=pid)["components"]}
    s = rows["demo.edit@1.0.0"]["summary"]
    assert "**Question.**" in s and "`edits`" in s and "Receipt" not in s
    assert rows["demo.own@1.0.0"]["summary"] == "its own summary"
    assert rows["demo.none@1.0.0"]["summary"] == ""
