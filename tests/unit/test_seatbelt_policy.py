"""Seatbelt profile generation and sealed-profile selection (host-independent; the macOS CI job runs
the generated profile under sandbox-exec with hostile-access probes: scripts/ci/seatbelt_probes.py)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from eidos.application.handlers.runs import choose_profile, strongest_profile
from eidos.domain.errors import Invalid
from eidos.domain.isolation import PROFILES, SEALED_PROFILES, evaluate_receipt
from eidos.runtime.launcher import LaunchSpec, profile_config_digest, seatbelt_argv, seatbelt_policy


def _spec(tmp_path, cwd=None):
    lab = tmp_path / "lab"
    adir = lab / "jobs" / "j" / "a"
    return LaunchSpec(argv=["/usr/bin/python3", "-c", "pass"], cwd=str(cwd or tmp_path / "project"),
                      attempt_dir=str(adir), profile="macos:seatbelt-sealed",
                      readonly=[str(adir / "invocation.json"), str(tmp_path / "sdk")],
                      writable=[str(adir / "staging"), str(adir / "scratch"), str(lab / "jobs" / "j" / "resume")],
                      deny_read=[str(lab), str(tmp_path / "home" / ".ssh")]), lab, adir


def _values(params):
    return dict(p.split("=", 1) for p in params)


def test_policy_denies_lab_and_credentials_with_attempt_exceptions(tmp_path):
    spec, lab, adir = _spec(tmp_path)
    text, params = seatbelt_policy(spec, str(adir / "tmp"))
    vals = _values(params)
    assert text.startswith("(version 1)\n(allow default)\n(deny network*)")
    assert text.count("(deny file-read-data file-read-xattr") == 2
    denied = [v for k, v in vals.items() if k.startswith("D")]
    assert str(lab.resolve()) in denied and str((tmp_path / "home" / ".ssh").resolve()) in denied
    exceptions = {v for k, v in vals.items() if k.startswith("R")}
    # only the attempt's own paths inside the lab are re-allowed; the SDK outside the lab needs none
    assert str((adir / "invocation.json").resolve()) in exceptions
    assert str((adir / "staging").resolve()) in exceptions and str((tmp_path / "sdk").resolve()) not in exceptions
    writes = {v for k, v in vals.items() if k.startswith("W")}
    assert writes == {str((adir / d).resolve()) for d in ("staging", "scratch", "tmp")} | {str((lab / "jobs" / "j" / "resume").resolve())}
    assert {v for k, v in vals.items() if k.startswith("L")} == {str((adir / f).resolve()) for f in ("stdout.log", "stderr.log")}
    assert '(subpath "/dev")' in text
    # paths travel as -D parameters, never spliced into the profile text
    assert str(lab) not in text


def test_ancestor_paths_do_not_reopen_the_lab(tmp_path):
    # a project root that contains the lab home must not re-allow reads inside it
    spec, lab, adir = _spec(tmp_path, cwd=tmp_path)
    _, params = seatbelt_policy(spec, str(adir / "tmp"))
    exceptions = {v for k, v in _values(params).items() if k.startswith("R")}
    assert str(tmp_path.resolve()) not in exceptions


def test_argv_and_digest_shape(tmp_path):
    spec, _, adir = _spec(tmp_path)
    argv = seatbelt_argv(spec, str(adir / "tmp"))
    assert argv[:2] == ["/usr/bin/sandbox-exec", "-p"] and argv[-3:] == spec.argv
    assert argv.count("-D") == len(seatbelt_policy(spec, str(adir / "tmp"))[1])
    d1 = profile_config_digest(spec)
    spec.deny_read = spec.deny_read[:1]
    assert profile_config_digest(spec) != d1  # the deny set is part of the recorded configuration


def _ctx(**settings):
    return SimpleNamespace(settings=settings, principal=SimpleNamespace(id="human:t"))


def test_strongest_profile_selection_and_waiver():
    assert "macos:seatbelt-sealed" in SEALED_PROFILES and "linux:bwrap-sealed" in SEALED_PROFILES
    assert "dev:same-user" not in SEALED_PROFILES
    assert strongest_profile(_ctx()) == "dev:same-user"
    assert strongest_profile(_ctx(sealed_available=True)) == "linux:bwrap-sealed"  # labs initialised before the field
    mac = _ctx(sealed_available=True, sealed_profile="macos:seatbelt-sealed")
    assert strongest_profile(mac) == "macos:seatbelt-sealed"
    assert choose_profile(mac, None, None)["profile"] == "macos:seatbelt-sealed"
    with pytest.raises(Invalid):
        choose_profile(mac, "dev:same-user", None)  # weaker than available: needs a recorded waiver
    assert choose_profile(mac, "dev:same-user", "debugging")["waiver"] == "debugging"


def test_seatbelt_receipt_cannot_claim_separate_identity():
    assert "separate-identity" not in PROFILES["macos:seatbelt-sealed"]["controls"]
    receipt = {"launcher": "svc-sealed-launcher", "profile": "macos:seatbelt-sealed",
               "controls": {c: "passed" for c in PROFILES["macos:seatbelt-sealed"]["controls"]} | {"separate-identity": "failed"}}
    # G3 (v1.0.0): Seatbelt runs candidate code as the same OS user, so its receipt can satisfy
    # control-level requirements but never a plan requiring sealed (hostile-code) execution.
    sealed = evaluate_receipt({"sealed": True}, receipt, trusted_launchers={"svc-sealed-launcher"})
    assert not sealed["ok"]
    assert any("same-OS-user receipt" in f["finding"] and f.get("same_os_user") is True for f in sealed["findings"])
    assert not evaluate_receipt({"profile": "linux:bwrap-sealed"}, receipt, trusted_launchers={"svc-sealed-launcher"})["ok"]
    controls_only = {"controls": list(PROFILES["macos:seatbelt-sealed"]["controls"])}
    assert evaluate_receipt(controls_only, receipt, trusted_launchers={"svc-sealed-launcher"})["ok"]
    need_identity = {"controls": ["no-label-read", "separate-identity"]}
    assert not evaluate_receipt(need_identity, receipt, trusted_launchers={"svc-sealed-launcher"})["ok"]


def test_seatbelt_lab_refuses_plans_requiring_sealed_execution():
    from eidos.domain.isolation import HOSTILE_CODE_PROFILES, profile_traits, requires_sealed, sealed_refusal
    assert "macos:seatbelt-sealed" not in HOSTILE_CODE_PROFILES and "linux:bwrap-sealed" in HOSTILE_CODE_PROFILES
    assert profile_traits("macos:seatbelt-sealed") == {"profile": "macos:seatbelt-sealed", "same_os_user": True,
                                                        "hostile_code_isolation": False}
    assert profile_traits("linux:bwrap-sealed")["hostile_code_isolation"] is True
    assert profile_traits("bogus")["same_os_user"] is True  # unknown profiles are never trusted as sealed
    assert requires_sealed({"sealed": True}) and requires_sealed({"profile": "linux:bwrap-sealed"})
    assert not requires_sealed({"profile": "dev:same-user", "controls": []}) and not requires_sealed(None)
    msg = sealed_refusal({"sealed": True}, "macos:seatbelt-sealed")
    assert msg and "same OS user" in msg and "linux:bwrap-sealed" in msg
    assert sealed_refusal({"sealed": True}, "dev:same-user")
    assert sealed_refusal({"sealed": True}, "linux:bwrap-sealed") is None
    assert sealed_refusal({"profile": "dev:same-user", "controls": []}, "macos:seatbelt-sealed") is None
