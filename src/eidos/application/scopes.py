"""Grant vocabulary and the default role → grant mapping.

Grants are checked by the command service; the processes holding them are separated by
credentials (and, for sealed work, by OS profiles). Declaring a grant in a component manifest
never confers it.
"""

ROLE_SCOPES: dict[str, list[str]] = {
    "researcher": [
        "project:read", "artifact:read", "artifact:write", "investigate", "steer", "control", "define_study",
        "amend_charter", "propose", "plan", "spend_compute", "invoke", "build", "publish_component",
        "manage_profile", "freeze_plan", "withdraw", "export", "promote_protocol", "qualify",
    ],
    # The internal investigator: may explore, author components and propose plans, but cannot
    # define or amend success rules, assess, qualify, promote or withdraw.
    "investigator": [
        "project:read", "artifact:read", "artifact:write", "propose", "plan", "spend_compute", "invoke", "build",
        "publish_component", "manage_profile", "export", "turn", "freeze_plan:policy",
    ],
    "assessor": ["project:read", "artifact:read", "assess"],
    "qualifier": ["project:read", "artifact:read", "qualify"],
    "launcher": ["project:read", "artifact:read", "artifact:write", "launcher"],
    # The LLM broker (served by the investigator worker, which holds provider keys; ADR 0013): it can only
    # reserve and settle calls of grants the launcher issued.
    "llm-broker": ["project:read", "llm_broker"],
    "workflow": ["project:read", "turn", "orchestrate"],
    "reader": ["project:read", "artifact:read"],
    "admin": ["admin"],
}


def scopes_for(roles: list[str]) -> list[str]:
    out: set[str] = set()
    for r in roles:
        if r not in ROLE_SCOPES:
            raise ValueError(f"unknown role {r!r}")
        out.update(ROLE_SCOPES[r])
    return sorted(out)
