"""Selectable prompt revisions; default selection preserves the historical exploration-policy switch."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eidos.domain.investigator_config import PROMPT_PACK_IDS
from eidos.investigator.context import TOOL_GUIDE, TOOL_GUIDE_V4
from eidos.investigator.playbook import PLAYBOOK, PLAYBOOK_ID, PLAYBOOK_V3

PROMPT_REVISION = "investigator-prompt/5"
SYSTEM = (
    "You are the internal investigator of a model-research lab. Diagnose failures, propose the most "
    "informative next experiment, author components when the catalog lacks a needed method, and stop "
    "with an honest scoped conclusion. Every hypothesis needs a prediction and a falsifier. You may call "
    "read-only tools to inspect evidence; effects happen only through the calls in your final decision, "
    "which the lab validates before execution. You cannot change success rules.\n\n" + PLAYBOOK
)



@dataclass(frozen=True)
class PromptPack:
    system: str
    playbook: str
    playbook_id: str
    tool_guide: str


PROMPT_PACKS: dict[str, PromptPack] = {}


def register_prompt_pack(revision: str, pack: PromptPack) -> None:
    PROMPT_PACKS[revision] = pack
    PROMPT_PACK_IDS.add(revision)


register_prompt_pack(PROMPT_REVISION, PromptPack(SYSTEM, PLAYBOOK, PLAYBOOK_ID, TOOL_GUIDE))
register_prompt_pack("investigator-prompt/6", PromptPack(
    SYSTEM.replace(PLAYBOOK, PLAYBOOK_V3), PLAYBOOK_V3, "investigator-playbook/3", TOOL_GUIDE_V4))


def select_prompt_pack(settings: dict[str, Any], view: dict[str, Any] | None = None) -> tuple[str, PromptPack]:
    config = ((view or {}).get("investigation", {}).get("body", {}).get("investigator") or {})
    v3 = (config.get("exploration") or {}).get("policy") == "exploration-policy/3"
    revision = settings.get("prompt_pack")
    if revision is None:
        revision = "investigator-prompt/6" if v3 else PROMPT_REVISION
    if revision not in PROMPT_PACKS:
        raise ValueError(f"unknown prompt pack {revision!r}; known packs: {', '.join(sorted(PROMPT_PACKS))}")
    return revision, PROMPT_PACKS[revision]
