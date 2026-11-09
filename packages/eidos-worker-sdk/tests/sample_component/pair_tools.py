from eidos_worker import check_finite


def run(inputs, config, *, reader, writer, context):
    counts = {}
    n = 0
    for row in reader.rows(inputs["predictions"]):
        n += 1
        g = counts.setdefault(row[config["group_by"]], [0, 0])
        g[0] += 1
        g[1] += int(row["pred_a"] == row["label_a"] and row["pred_b"] == row["label_b"])
    context.record("input_rows", n)
    context.logger.info("scored %d rows", n)
    writer.stage_json("report", counts, schema="grouped-pair-counts/1")


def only_context(inputs, config, context):
    context.record("seed", context.seed)
    return {"returned": True}


def boom(inputs, config):
    raise ValueError("kaput")


def nan(inputs, config):
    check_finite([1.0, float("nan")], "loss")


def oom(inputs, config):
    class OutOfMemoryError(RuntimeError):
        pass

    raise OutOfMemoryError("CUDA out of memory")


def mem(inputs, config):
    raise MemoryError("no")


def sneaky(inputs, config, *, reader):
    reader.open("/etc/passwd")


def big(inputs, config, *, writer):
    writer.stage_file("a", b"x" * 100)
    writer.stage_file("b", b"x" * 100)
