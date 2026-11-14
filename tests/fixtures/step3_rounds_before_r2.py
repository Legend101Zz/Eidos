"""Frozen rounds() from 89e79e8 for differential checks of recorded development ledgers."""
from typing import Any

from eidos.domain.exploration import (
    CHALLENGER_PURPOSES,
    TERMINAL_STATUSES,
    _metrics,
    lineage,
    metric_value,
    selection_evidence,
)


def rounds(round_log: list[dict[str, Any]], jobs: list[dict[str, Any]], first_lock_turn: int | None,
           last_change_turn: int | None, excluded: set[str] | None = None, rule: dict[str, Any] | None = None,
           held_out: set[str] | None = None, *, v3: bool = False) -> tuple[list[int], int, list[str]]:
    """(the challenger rounds: turns after the first lock that admitted at least one experiment with a challenger
    purpose that is not reproduction work (``excluded``); the number of those after the incumbent last changed that
    are **evaluated**; the challenger jobs of rounds still pending). R14 #6: a round counts as unsuccessful only once
    every challenger it admitted is terminal and, when it succeeded, has selection evidence under ``rule`` (a failed
    challenger is evaluated: it reports nothing more); a pending round may finish.
    ``round_log`` lists, per turn, the job ids its decisions admitted: ``[{"turn": seq, "jobs": [ids]}]``."""
    if first_lock_turn is None:
        return [], 0, []
    by_id = {j.get("id"): j for j in jobs}
    skip = excluded or set()
    per_turn: dict[int, list[str]] = {}
    for r in round_log:
        if int(r["turn"]) <= first_lock_turn:
            continue
        ids = [str(j) for j in r.get("jobs") or [] if ((by_id.get(j) or {}).get("purpose") in CHALLENGER_PURPOSES
               or (v3 and str((by_id.get(j) or {}).get("component", "")).startswith("eidos.search.recipe-portfolio")
                   and (by_id.get(j) or {}).get("purpose") == "reproduction"))
               and j not in skip
               and not (v3 and str((by_id.get(j) or {}).get("component", "")).startswith("eidos.diagnose."))]
        if ids:
            per_turn.setdefault(int(r["turn"]), []).extend(ids)
    turns = sorted(per_turn)

    def evaluated(jid: str) -> bool:
        j = by_id.get(jid) or {}
        if j.get("status") not in TERMINAL_STATUSES:
            return False
        if v3 and j.get("status") == "succeeded":
            if str(j.get("component", "")).split("@")[0] == "eidos.search.recipe-portfolio" and metric_value(_metrics(j), "round_evaluated") == 1.0:
                return True
            # A support job consumes an evidence-bearing ancestor's outputs. lineage() walks descendants.
            if rule is not None and any(selection_evidence(jobs, str(parent["id"]), rule, held_out) is not None
                                        and any(child.get("id") == jid for child in lineage(jobs, str(parent["id"])))
                                        for parent in jobs):
                return True
        return j.get("status") != "succeeded" or (rule is not None and selection_evidence(jobs, jid, rule, held_out)
                                                  is not None)
    pending = [jid for t in turns for jid in per_turn[t] if not evaluated(jid)]
    since = [t for t in turns if (last_change_turn is None or t > last_change_turn)
             and all(evaluated(jid) for jid in per_turn[t])]
    return turns, len(since), pending
