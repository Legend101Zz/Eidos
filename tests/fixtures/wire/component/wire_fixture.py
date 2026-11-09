"""Fixture component for the wire golden fixtures (tests/fixtures/wire). Python 3.9+, stdlib only."""


def run(inputs, config, reader, writer, context):
    rows = list(reader.rows("rows"))
    scale = int(config.get("scale", 1))
    context.logger.info("read %d rows", len(rows))
    writer.stage_json("summary", {"rows": len(rows), "scale": scale}, schema="row-summary/1")
    writer.stage_rows("rows_out", [{"a": int(r["a"]) * scale, "b": r["b"]} for r in rows], schema="labeled-rows/1")
    return {"input_rows": len(rows)}


def fail(inputs, config, context):
    context.logger.info("about to fail")
    raise ValueError("fixture failure: the component raised on purpose")
