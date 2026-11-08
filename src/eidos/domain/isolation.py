"""Realized isolation and assessment eligibility (design §"Realized isolation").

Profiles are sets of controls, not a ranking. A plan states the controls it requires; a trusted
launcher's receipt records per-control ``passed``/``failed``/``unknown`` from executed probes.
"""

from __future__ import annotations

from typing import Any

SEALED_CONTROLS = (
    "no-label-read",
    "no-credential-read",
    "no-host-write",
    "no-network-egress",
    "no-control-socket",
    "no-scorer-capability",
)
# Reported by every sealed receipt; required only when a plan lists it explicitly ("distinct
# service identities where supported").
OPTIONAL_CONTROLS = ("separate-identity", "no-process-inspection")

PROFILES: dict[str, dict[str, Any]] = {
    "dev:same-user": {
        "controls": (),
        "summary": "host-native, same OS user; exploratory work only",
        "platforms": ("linux", "darwin"),
        "same_os_user": True,
        "hostile_code": False,
    },
    "linux:bwrap-sealed": {
        "controls": SEALED_CONTROLS + OPTIONAL_CONTROLS,
        "summary": "bubblewrap namespaces, read-only inputs, no network, separate scorer process",
        "platforms": ("linux",),
        "same_os_user": False,  # own user/PID/mount/network namespaces; optionally a distinct OS identity
        "hostile_code": True,
    },
    "macos:seatbelt-sealed": {
        "controls": SEALED_CONTROLS + ("no-process-inspection",),
        "summary": "macOS Seatbelt (sandbox-exec): lab home and credential directories unreadable, writes only to "
                   "staging/scratch/resume, no network, cleared environment; same OS user; mach IPC not filtered",
        "platforms": ("darwin",),
        # Same OS user: launchd/XPC and direct mach messages stay reachable, so a hostile same-user
        # program is not contained (docs/release/SUPPORTED.md, ADR 0006). Not hostile-code isolation.
        "same_os_user": True,
        "hostile_code": False,
    },
    "macos:lima-vm-sealed": {
        "controls": SEALED_CONTROLS,
        "summary": "Lima Linux VM with bwrap inside; no broad host mounts (not implemented; not supported in v1)",
        "platforms": ("darwin",),
        "same_os_user": False,
        "hostile_code": True,
    },
}
SEALED_PROFILES = tuple(p for p in PROFILES if p.endswith("-sealed"))
# Profiles that contain hostile candidate code. Only these satisfy a plan requiring sealed execution.
HOSTILE_CODE_PROFILES = tuple(p for p, v in PROFILES.items() if v["hostile_code"])
SAME_USER_SEALED_MESSAGE = (
    "a same-OS-user receipt ({profile}) cannot satisfy a plan requiring sealed (hostile-code) execution; in v1 "
    "sealed evaluation is supported on Linux with linux:bwrap-sealed only (docs/release/SUPPORTED.md)")


def profile_traits(profile: str | None) -> dict[str, Any]:
    """What a profile is, independent of what its receipt claims. Unknown profiles are treated as
    same-user and not hostile-code capable."""
    meta = PROFILES.get(profile or "", {})
    return {"profile": profile, "same_os_user": bool(meta.get("same_os_user", True)),
            "hostile_code_isolation": bool(meta.get("hostile_code", False))}


def requires_sealed(required: dict[str, Any] | None) -> bool:
    """A plan requires sealed (hostile-code) execution when it says ``sealed: true`` or names a
    hostile-code profile. A plan listing individual controls only is a control-level requirement."""
    required = required or {}
    return bool(required.get("sealed")) or required.get("profile") in HOSTILE_CODE_PROFILES


def sealed_refusal(required: dict[str, Any] | None, strongest_profile: str) -> str | None:
    """Clear message when this lab cannot run a plan that requires sealed execution, else None."""
    if not requires_sealed(required) or strongest_profile in HOSTILE_CODE_PROFILES:
        return None
    return ("this plan requires sealed (hostile-code) execution, but this lab's strongest isolation profile is "
            f"{strongest_profile!r}, which runs candidate code as the same OS user; in v1 sealed evaluation is "
            "supported on Linux with linux:bwrap-sealed only (docs/release/SUPPORTED.md)")


def evaluate_receipt(required: dict[str, Any], receipt: dict[str, Any] | None, *, trusted_launchers: set[str]) -> dict[str, Any]:
    """Decide whether a realized isolation receipt satisfies a plan's requirement."""
    findings: list[dict[str, Any]] = []
    need = list(required.get("controls") or [])
    if requires_sealed(required) and not need:
        need = list(SEALED_CONTROLS)
    # A named profile other than same-user is a requirement even without listed controls.
    if not need and required.get("profile") in (None, "", "dev:same-user"):
        return {"ok": True, "findings": []}
    if not receipt:
        return {"ok": False, "findings": [{"finding": "no isolation receipt; required controls unverified",
                                            "required": need}]}
    if receipt.get("launcher") not in trusted_launchers:
        findings.append({"finding": "receipt not issued by a trusted launcher", "launcher": receipt.get("launcher")})
    want_profile = required.get("profile")
    if want_profile and receipt.get("profile") != want_profile:
        findings.append({"finding": f"receipt profile {receipt.get('profile')!r} differs from required {want_profile!r}"})
    if receipt.get("profile") == "dev:same-user":
        findings.append({"finding": "same-user development receipt cannot satisfy a plan requiring sealed execution"})
    elif requires_sealed(required) and receipt.get("profile") not in HOSTILE_CODE_PROFILES:
        findings.append({"finding": SAME_USER_SEALED_MESSAGE.format(profile=receipt.get("profile")),
                         **profile_traits(receipt.get("profile"))})
    controls = receipt.get("controls") or {}
    for c in need:
        state = controls.get(c, "unknown")
        if state != "passed":
            findings.append({"finding": f"control {c} is {state}", "control": c})
    return {"ok": not findings, "findings": findings}
