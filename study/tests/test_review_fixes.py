"""Regression coverage for the independent WP2 review (public inputs only)."""

import base64
import hashlib
import json
import os
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from study.eidos_arms import arm4, lab, provider_bridge
from study.exp import flags
from study.exp.treatment import resolve
from study.tests.test_default_treatment_golden import BUNDLES, INVESTIGATOR_SHA


def bundle_digest(directory):
    files = {}
    for path in sorted(directory.rglob("*")):
        if (
            path.is_file()
            and not path.is_symlink()
            and "__pycache__" not in path.parts
            and not path.name.endswith(".pyc")
        ):
            data = path.read_bytes()
            try:
                files[str(path.relative_to(directory))] = data.decode()
            except UnicodeDecodeError:
                files[str(path.relative_to(directory))] = {"base64": base64.b64encode(data).decode()}
    return hashlib.sha256(json.dumps({"files": files}, sort_keys=True).encode()).hexdigest()


def test_m1_removal_dry_run_detects_shared_portfolio():
    plan = flags.removal_list(assume_rejected=["floor.v2-slice"])
    portfolio = next(
        row
        for row in plan
        for row in row["paths"]
        if row["path"] == "components/eidos.search.recipe-portfolio/"
    )
    assert portfolio["blocked_by"] == ["flag:toolkit.portfolio"]
    assert portfolio["removable"] is False


def test_m2_v2_publishes_previous_diagnosis_release():
    # Failing-test-first smoke; the complete step-3 digest golden follows below.
    plan = resolve("v2-portfolio").component_plan("N01")
    assert any(path.name == "eidos.diagnose.baseline-errors@1.0.0" and not bind for path, bind in plan)


@pytest.mark.parametrize("selection", ["pilot4-v1", "v2-portfolio"])
def test_m3_arm4_records_effective_configuration(tmp_path, monkeypatch, selection):
    monkeypatch.setenv("EIDOS_STUDY_TREATMENT", selection)
    treatment = resolve()
    meta = {"treatment": treatment.record(), "card": "N02", "envelope": {"wall_s": 20}}
    (tmp_path / "task").mkdir()
    (tmp_path / "transcript.jsonl").write_text("")
    client = Mock()
    client.command.return_value = {"result": {"investigation_id": "inv"}}
    shown = {"status": "stopped", "body": {}, "turns": []}
    client.query.return_value = shown
    bridge = SimpleNamespace(poll=lambda: 0, pid=1, returncode=0)
    records = {"assessment": [], "evidence_plan": []}
    with ExitStack() as stack:
        for obj, name, value in [
            (lab, "start", None),
            (lab, "guard_disk", None),
            (lab, "client", client),
            (arm4, "prepare", ("project", {"charter_id": "charter", "artifacts": {}, "task": {}})),
            (arm4, "prompt", "public fixture"),
            (arm4, "ledger", (shown, [], records)),
            (arm4, "usage", ({}, [], None, [])),
            (arm4, "select_job", ("job", "fixture")),
            (arm4, "candidate_provenance", {}),
            (arm4, "export_answer", "fixture"),
            (arm4, "validate", True),
            (arm4, "classify", ("ok", None)),
            (provider_bridge, "start", (bridge, {"upstream": "fixture", "base_url": "fixture"})),
        ]:
            stack.enter_context(patch.object(obj, name, return_value=value))
        result = arm4.run(tmp_path, meta)
    effective = hashlib.sha256(
        json.dumps(treatment.investigator, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    assert result["investigator_effective_sha256"] == effective
    assert result["investigator_config_sha"] == (INVESTIGATOR_SHA if selection == "pilot4-v1" else effective)
    assert result["treatment"] == treatment.record()
    assert json.loads((tmp_path / "setup.json").read_text())["treatment"] == treatment.record()
    used = next(
        call.args[1]["investigator"]
        for call in client.command.call_args_list
        if call.args[0] == "start_investigation"
    )
    for key in ("context_policy", "exploration"):
        assert used.get(key) == treatment.investigator.get(key)


# Replayed the install loop in git show 44cc94a:study/eidos_arms/common.py,
# hashing its archived bundles with main's bundle_dir encoding. Only portfolio's
# manifest dependency declaration and preflight method-card note change its digest.
STEP3_BUNDLES = [
    BUNDLES[0],
    (
        "eidos.diagnose.baseline-errors@1.1.0",
        "58bbb0f5e088c7a4c3ac75f3ba2ad33bf263cd5f325e5dc451f08343c141b408",
        True,
    ),
    (BUNDLES[1][0], BUNDLES[1][1], False),
    (
        "eidos.search.recipe-portfolio@1.0.0",
        "4b02e49fd939d0856c16b2adee42ad51249eca19eb27cad4c3c523202dfdc3a0",
        True,
    ),
    *BUNDLES[2:],
    (
        "eidos.task.score-regression@1.0.2",
        "e0f456b7323cff4e7a97094960d1bc7103742c2046d02a9f20b19e42d1053222",
        True,
    ),
]


@pytest.mark.parametrize("card", ["N01", "N02", "D03"])
def test_v2_exact_step3_component_plan(card):
    expected = [
        entry for entry in STEP3_BUNDLES if card != "N02" or not entry[0].startswith("eidos.task.paired-")
    ]
    expected = [
        (
            ref,
            "bd52de8553c1c14302959496c8c333c8cd62706fe7159945711d9d0dbcaf59c5"
            if ref.startswith("eidos.search.recipe-portfolio@")
            else digest,
            bind,
        )
        for ref, digest, bind in expected
    ]
    actual = []
    for path, bind in resolve("v2-portfolio").component_plan(card):
        manifest = json.loads((path / "eidos-component.json").read_text())
        actual.append((manifest["id"] + "@" + manifest["release"], bundle_digest(path), bind))
    assert actual == expected


def test_default_selection_unset_and_new_identity(monkeypatch):
    monkeypatch.delenv("EIDOS_STUDY_TREATMENT", raising=False)
    assert resolve().id == "pilot4-v1"
    diagnosis = resolve().component_plan("N01")[1]
    assert diagnosis.upload_name == "eidos.diagnose.baseline-errors"
    assert resolve().sha256 == "75ca475e47e26acfaa37dcd8789dd24b2a242a8cb11fad491dce0a3b3202e088"


def test_live_dispatch_requires_record(monkeypatch):
    from study.exp.treatment import for_run

    monkeypatch.setenv("EIDOS_STUDY_LIVE_DISPATCH", "1")
    with pytest.raises(ValueError, match="requires.*treatment"):
        for_run({})


def test_toolkit_manifest_identity_matches_actual_export(tmp_path):
    from study.exp.artifacts import toolkit_manifest_sha256
    from study.toolkit.build_toolkit import build_toolkit

    for selection in ("pilot4-v1", "v2-portfolio"):
        treatment = resolve(selection)
        toolkit = build_toolkit(tmp_path / selection, treatment=treatment)
        assert (
            toolkit_manifest_sha256(treatment)
            == hashlib.sha256((toolkit / "manifest.json").read_bytes()).hexdigest()
        )


@pytest.mark.parametrize("change", ["unknown", "default_override", "default_flag"])
def test_strict_treatment_definition(tmp_path, monkeypatch, change):
    definition = json.loads((flags.TREATMENTS / "pilot4-v1.json").read_text())
    if change == "unknown":
        definition["typo"] = True
    elif change == "default_override":
        definition["overrides"] = {"max_turns": 1}
    else:
        definition["flags"] = ["diagnose.steering-fix"]
    (tmp_path / "pilot4-v1.json").write_text(json.dumps(definition))
    monkeypatch.setattr(flags, "TREATMENTS", tmp_path)
    with pytest.raises(ValueError, match="treatment"):
        resolve("pilot4-v1")


def test_unknown_patch_keys_and_runtime_code_inventory(tmp_path, monkeypatch):
    registry = flags.load_registry()
    registry["flags"]["floor.v2-slice"]["patch"]["toolkits"] = {}
    with pytest.raises(ValueError, match="unknown patch"):
        flags.validate_registry(registry)
    del registry["flags"]["floor.v2-slice"]["patch"]["toolkits"]
    registry["flags"]["floor.v2-slice"]["code"].append({"path": "tests/not-shipped.py", "kind": "delete"})
    flags.validate_registry(registry, check_paths=False)
    with pytest.raises(ValueError, match="missing code"):
        flags.validate_registry(registry)
    registry_path = tmp_path / "flags.json"
    registry_path.write_text(json.dumps(registry))
    monkeypatch.setattr(flags, "REGISTRY", registry_path)
    assert resolve("pilot4-v1").id == "pilot4-v1"


@pytest.mark.parametrize(
    "rejected", ["diagnose.steering-fix", "floor.v2-slice", "floor.selection-margin", "toolkit.portfolio"]
)
def test_rejection_and_actual_removal_preserve_default(tmp_path, rejected):
    import shutil
    import subprocess
    import sys

    root = flags.ROOT
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
    for name in ("study", "components", "packages", "src", "tests"):
        shutil.copytree(root / name, tmp_path / name, ignore=ignore)
    shutil.copyfile(root / "LICENSE", tmp_path / "LICENSE")
    registry_path = tmp_path / "study/exp/flags.json"
    registry = json.loads(registry_path.read_text())
    registry["flags"][rejected].update(
        state="rejected", removal=dict(decided="2026-11-14", experiment="test", reason="test")
    )
    registry_path.write_text(json.dumps(registry))
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-m", "study.exp.flags", "removal-list"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )
    plan = json.loads(result.stdout)
    row = plan[0]
    assert row["id"] == rejected
    assert row["treatments"] == ["v2-portfolio"]
    assert [entry["path"] for entry in row["paths"]] == [
        entry["path"] for entry in registry["flags"][rejected]["code"]
    ]
    if rejected == "diagnose.steering-fix":
        assert len(row["paths"]) == 1
        assert row["paths"][0]["kind"] == "procedure"
        assert "Restore the 1.0.0" in row["paths"][0]["text"]
        assert not row["paths"][0]["removable"]
    elif rejected == "floor.selection-margin":
        assert row["paths"] == []
    elif rejected == "floor.v2-slice":
        assert row["blocked_by"] == ["flag:floor.selection-margin"]
        assert all(entry["removable"] for entry in row["paths"] if "recipe-portfolio" not in entry["path"])
    if rejected in ("toolkit.portfolio", "floor.v2-slice"):
        shared = next(
            entry for entry in row["paths"] if entry["path"] == "components/eidos.search.recipe-portfolio/"
        )
        assert not shared["removable"] and shared["blocked_by"]
        assert result.returncode == 1
    else:
        assert result.returncode == 0, result.stderr
    for entry in row["paths"]:
        path = tmp_path / entry["path"]
        if entry["removable"]:
            shutil.rmtree(path) if path.is_dir() else path.unlink()
        elif entry["kind"] == "procedure":
            shutil.rmtree(path)
            shutil.copytree(tmp_path / "components/eidos.diagnose.baseline-errors@1.0.0", path)
    # Archive dependent flags/treatments as indicated by the plan, then remove the
    # rejected registry entry. Shared code is retained for its surviving owner.
    removed = {rejected}
    while True:
        expanded = removed | {
            name for name, flag in registry["flags"].items() if set(flag["requires"]) & removed
        }
        if expanded == removed:
            break
        removed = expanded
    for path in (tmp_path / "study/exp/treatments").glob("*.json"):
        if set(json.loads(path.read_text())["flags"]) & removed:
            path.unlink()
    registry["flags"] = {name: flag for name, flag in registry["flags"].items() if name not in removed}
    for flag in registry["flags"].values():
        flag["recommends"] = [name for name in flag.get("recommends", []) if name not in removed]
        for entry in flag["code"]:
            if isinstance(entry, dict):
                entry["shared_with"] = [name for name in entry.get("shared_with", []) if name not in removed]
    registry_path.write_text(json.dumps(registry))
    for command in (
        [sys.executable, "-m", "study.exp.flags", "check"],
        [sys.executable, "-m", "study.exp.flags", "show", "pilot4-v1"],
        [
            sys.executable,
            "-m",
            "unittest",
            "study.tests.test_default_treatment_golden.DefaultTreatmentGolden.test_base_investigator_is_main_bytes",
            "study.tests.test_default_treatment_golden.DefaultTreatmentGolden.test_exact_component_refs_bundle_bytes_order_and_bindings",
        ],
    ):
        checked = subprocess.run(command, cwd=tmp_path, env=env, text=True, capture_output=True)
        assert checked.returncode == 0, checked.stdout + checked.stderr


@pytest.mark.parametrize("selection", ["pilot4-v1", "v2-portfolio"])
def test_launcher_and_lab_forward_selection_and_arm_record(tmp_path, monkeypatch, selection):
    from study import launcher
    from study.common import MODEL, read_json, write_json

    monkeypatch.setenv("EIDOS_STUDY_TREATMENT", selection)
    monkeypatch.setenv("EIDOS_STUDY_HOME", str(tmp_path / "lab"))
    monkeypatch.setenv("EIDOS_PILOT_CHARTER_COMMIT", "a" * 40)
    monkeypatch.setenv("EIDOS_PILOT_CHARTER_PATH", "docs/experiments/pilot-1/CHARTER.md")
    treatment = resolve()
    assert lab.environment()["EIDOS_STUDY_TREATMENT"] == selection
    assert lab.environment()["OMP_NUM_THREADS"] == "1"

    def package(*args):
        assert args[0] == "package_task.py"
        target = Path(args[args.index("--out") + 1])
        target.mkdir(parents=True)

    monkeypatch.setattr(launcher, "dev_command", package)
    monkeypatch.setattr(
        launcher.subprocess,
        "check_output",
        lambda *a, **kw: "Filesystem Size Used Available\nfixture 0 0 999999999\n",
    )

    def process(command, **kwargs):
        assert kwargs["env"]["EIDOS_STUDY_TREATMENT"] == selection
        assert kwargs["env"]["EIDOS_STUDY_LIVE_DISPATCH"] == "1"
        run_dir = Path(command[command.index("--run-dir") + 1])
        meta = read_json(run_dir / "arm.json")
        assert meta["treatment"] == treatment.record()
        write_json(
            run_dir / "run_record.json",
            {"status": "ok", "model_ids_seen": [MODEL], "treatment": meta["treatment"]},
        )
        return SimpleNamespace(poll=lambda: 0, returncode=0)

    monkeypatch.setattr(launcher.subprocess, "Popen", process)
    monkeypatch.setattr(launcher, "research_python", lambda: "fixture-python")
    rows = [dict(arm=4, task="N02", run=1, block_id="N02-r1")]
    block = launcher.run_block(rows, tmp_path / "runs")
    assert block["status"] == "ok"
    directory = Path(block["run_dirs"]["4"])
    for filename in ("arm.json", "run_record.json"):
        assert read_json(directory / filename)["treatment"] == treatment.record()


def test_configuration_identity_tracks_real_source_changes(tmp_path, monkeypatch):
    import shutil

    from study.exp import treatment as resolver

    root = flags.ROOT
    for name in (
        "components",
        "packages",
        "src",
        "study/exp",
        "study/eidos_arms",
        "study/prompts",
        "study/toolkit",
    ):
        shutil.copytree(
            root / name, tmp_path / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
        )
    shutil.copyfile(root / "LICENSE", tmp_path / "LICENSE")
    monkeypatch.setattr(flags, "ROOT", tmp_path)
    monkeypatch.setattr(flags, "REGISTRY", tmp_path / "study/exp/flags.json")
    monkeypatch.setattr(flags, "TREATMENTS", tmp_path / "study/exp/treatments")
    monkeypatch.setattr(resolver, "ROOT", tmp_path)
    base, v2 = resolve("pilot4-v1").sha256, resolve("v2-portfolio").sha256
    source = tmp_path / "components/eidos.search.recipe-portfolio/portfolio.py"
    source.write_text(source.read_text() + "\n# changed implementation\n")
    assert resolve("v2-portfolio").sha256 != v2
    assert resolve("pilot4-v1").sha256 == base
    monkeypatch.setattr(
        resolver, "BASE_COMPONENTS", (*resolver.BASE_COMPONENTS, "eidos.task.score-regression")
    )
    assert resolve("pilot4-v1").sha256 != base
    before = resolve("pilot4-v1").sha256
    source = tmp_path / "study/toolkit/README.md"
    source.write_text(source.read_text() + "\nchanged toolkit\n")
    assert resolve("pilot4-v1").sha256 != before


def test_check_builds_plans_and_warns_on_missing_recommendation(tmp_path, monkeypatch, capsys):
    definition = dict(
        schema="eidos-treatment/1", id="no-margin", summary="test", flags=["floor.v2-slice"], overrides={}
    )
    path = tmp_path / "no-margin.json"
    path.write_text(json.dumps(definition))
    monkeypatch.setattr(flags, "TREATMENTS", tmp_path)
    assert flags.main(["check"]) == 0
    assert "WARN no-margin: floor.v2-slice recommends floor.selection-margin" in capsys.readouterr().out
    registry = flags.load_registry()
    registry["flags"]["floor.v2-slice"]["patch"]["components"]["install"].append("missing-component")
    monkeypatch.setattr(flags, "load_registry", lambda: registry)
    with pytest.raises(SystemExit) as caught:
        flags.main(["check"])
    assert caught.value.code == 1


def test_removal_reports_core_imports_and_unselected_flag_paths(tmp_path, monkeypatch):
    # A boolean guard is not a policy gate; relative imports count as core users.
    source = tmp_path / "src/eidos/core.py"
    source.parent.mkdir(parents=True)
    source.write_text("if True:\n    from .domain import promotion\n")
    monkeypatch.setattr(flags, "ROOT", tmp_path)
    assert flags._core_imports("src/eidos/domain/promotion.py", {"src/eidos/domain/promotion.py"}) == [
        "core import:src/eidos/core.py:2"
    ]
    source.write_text(
        'if config.get("policy") == "exploration-policy/3":\n    from .domain import promotion\n'
    )
    assert flags._core_imports("src/eidos/domain/promotion.py", {"src/eidos/domain/promotion.py"}) == []
    source.write_text(
        'if config.get("policy") == "exploration-policy/3":\n    pass\nelse:\n    from .domain import promotion\n'
    )
    assert flags._core_imports("src/eidos/domain/promotion.py", {"src/eidos/domain/promotion.py"}) == [
        "core import:src/eidos/core.py:4"
    ]


def test_removal_checks_patch_users_even_without_treatments():
    registry = flags.load_registry()
    # An active flag remains a user even when no active treatment selects it,
    # and when its code inventory forgot the shared declaration.
    registry["flags"]["toolkit.portfolio"]["code"] = []
    plan = flags.removal_list(registry, assume_rejected=["floor.v2-slice"])
    shared = next(
        entry
        for row in plan
        for entry in row["paths"]
        if entry["path"] == "components/eidos.search.recipe-portfolio/"
    )
    assert shared["blocked_by"] == ["flag:toolkit.portfolio"]


def test_removal_retains_core_modules_for_surviving_policy_overrides(tmp_path, monkeypatch):
    import shutil

    shutil.copyfile(flags.TREATMENTS / "pilot4-v1.json", tmp_path / "pilot4-v1.json")
    (tmp_path / "custom.json").write_text(
        json.dumps(
            dict(
                schema="eidos-treatment/1",
                id="custom",
                summary="custom",
                flags=[],
                overrides={"exploration": {"policy": "exploration-policy/3"}},
            )
        )
    )
    monkeypatch.setattr(flags, "TREATMENTS", tmp_path)
    plan = flags.removal_list(assume_rejected=["floor.v2-slice"])
    promotion = next(
        entry for row in plan for entry in row["paths"] if entry["path"] == "src/eidos/domain/promotion.py"
    )
    assert not promotion["removable"]
    assert any("core import:" in blocker for blocker in promotion["blocked_by"])
