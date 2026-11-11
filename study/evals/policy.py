"""Prospective Tier-1 comparison and audit policy."""

POLICY = {
    "id": "tier1-no-regression/1",
    "tolerance": 0.0,
    "absolute_float_epsilon": 1e-12,
    "quality": "Per assignment verified gain in native units and ratio where the frozen reference exists.",
    "invariants": ["dead_end", "fabricated_empty_wait", "held_out_reads"],
    "missing_evidence": "Unknown required invariant evidence or an absent verified gain blocks the gate.",
}
