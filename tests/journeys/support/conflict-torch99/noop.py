def run(inputs, config, *, writer, context):
    writer.stage_json("report", {}, schema="journey-noop-report/1")
    return {}
