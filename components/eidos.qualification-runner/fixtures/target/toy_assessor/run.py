"""Fixture-only toy assessor for the qualification runner's own author test (not a real method)."""


def run(inputs, config, *, reader, writer):
    effect = reader.json(inputs["evidence"])["effect"]
    writer.stage_json("evaluation", {"outcome": "protocol-passed" if effect > 0.5 else "protocol-failed"})
