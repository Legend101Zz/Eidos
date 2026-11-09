def run(inputs, config, *, writer, context):
    context.record("metrics", {"value": config.get("value", 0)})
    writer.stage_json("report", {"echo": config}, schema="echo-report/1")
    return {"echoed": True}
