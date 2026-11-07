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
OPTIONAL_CONTROLS = ("separate-identity",)

PROFILES: dict[str, dict[str, Any]] = {
    "dev:same-user": {
        "controls": (),
        "summary": "host-native, same OS user; exploratory work only",
        "platforms": ("linux", "darwin"),
    },
    "linux:bwrap-sealed": {
        "controls": SEALED_CONTROLS + OPTIONAL_CONTROLS,
        "summary": "bubblewrap namespaces, read-only inputs, no network, separate scorer process",
        "platforms": ("linux",),
    },
    "macos:lima-vm-sealed": {
        "controls": SEALED_CONTROLS,
        "summary": "Lima Linux VM with bwrap inside; no broad host mounts",
        "platforms": ("darwin",),
    },
}


def evaluate_receipt(required: dict[str, Any], receipt: dict[str, Any] | None, *, trusted_launchers: set[str]) -> dict[str, Any]:
    """Decide whether a realized isolation receipt satisfies a plan's requirement."""
    findings: list[dict[str, Any]] = []
    need = list(required.get("controls") or [])
    if required.get("sealed") and not need:
        need = list(SEALED_CONTROLS)
    if not need:
        return {"ok": True, "findings": []}
    if not receipt:
        return {"ok": False, "findings": [{"finding": "no isolation receipt; required controls unverified",
                                            "required": need}]}
    if receipt.get("launcher") not in trusted_launchers:
        findings.append({"finding": "receipt not issued by a trusted launcher", "launcher": receipt.get("launcher")})
    if receipt.get("profile") == "dev:same-user":
        findings.append({"finding": "same-user development receipt cannot satisfy a plan requiring sealed execution"})
    controls = receipt.get("controls") or {}
    for c in need:
        state = controls.get(c, "unknown")
        if state != "passed":
            findings.append({"finding": f"control {c} is {state}", "control": c})
    return {"ok": not findings, "findings": findings}
