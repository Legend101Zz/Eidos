"""General recipe search over a project command (standard library only, Python 3.9+).

Runs one command per recipe x seed x fold in a private working directory, using visible training and validation data
only, and writes a ranked evidence table with every evaluated recipe and the best one. Exploratory: no claim and no test
data. Lock the winner before independent confirmation.
"""
import hashlib
import itertools
import json
import math
import random
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

TABLE_SCHEMA = "recipe-sweep-table/1"
BEST_SCHEMA = "recipe-sweep-best/1"
# The only ports this search reads: a test or held-out split has no way in, through the manifest or a lab-free driver.
ALLOWED_INPUTS = ("source", "train", "validation", "task")
SUFFIX_BY_MEDIA = {"application/x-npz": ".npz", "application/json": ".json", "text/csv": ".csv", "text/plain": ".txt"}
MAX_COPY_BYTES = 2 << 20
MAX_RANDOM_DRAWS = 100000
TAIL_CHARS = 1500
_PARAM = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_.]*\}")
_HELDOUT = re.compile(r"(^|[^a-z0-9])(test|hold[_\- ]?out|held[_\- ]?out)([^a-z0-9]|$)")
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")


class SweepConfigError(ValueError):
    """The sweep's configuration is unusable; nothing was run."""


def _names_heldout(name):
    return bool(_HELDOUT.search(_CAMEL.sub(r"\1_\2", str(name)).lower()))


# -- the search space ----------------------------------------------------------------------------------------------
def _parameters(space):
    params = space.get("parameters")
    if not isinstance(params, dict) or not params:
        raise SweepConfigError("space.parameters must name at least one parameter")
    for name in params:
        if not _PARAM.match(name):
            raise SweepConfigError("parameter name %r must match %s" % (name, _PARAM.pattern))
    return [(name, params[name]) for name in sorted(params)]


def _grid(params):
    for name, spec in params:
        if not isinstance(spec.get("values"), list) or not spec["values"]:
            raise SweepConfigError("grid parameter %r needs a non-empty values list" % name)
    names = [n for n, _ in params]
    return (dict(zip(names, combo)) for combo in itertools.product(*[s["values"] for _, s in params]))


def _draw(rng, spec):
    if "values" in spec:
        return rng.choice(spec["values"])
    low, high = float(spec["low"]), float(spec["high"])
    if spec.get("log"):
        value = math.exp(rng.uniform(math.log(low), math.log(high)))
    else:
        value = rng.uniform(low, high)
    return int(round(value)) if spec.get("integer") else value


def _random(params, seed, samples):
    for name, spec in params:
        has_range = "low" in spec and "high" in spec
        if not (isinstance(spec.get("values"), list) and spec["values"]) and not has_range:
            raise SweepConfigError("random parameter %r needs values, or low and high" % name)
        if has_range and spec.get("log") and float(spec["low"]) <= 0:
            raise SweepConfigError("log-scale parameter %r needs low > 0" % name)
        if has_range and float(spec["low"]) > float(spec["high"]):
            raise SweepConfigError("parameter %r has low above high" % name)
    rng, seen, produced = random.Random(seed), set(), 0
    for _ in range(MAX_RANDOM_DRAWS):
        if samples is not None and produced >= samples:
            return
        recipe = {name: _draw(rng, spec) for name, spec in params}
        key = json.dumps(recipe, sort_keys=True)
        if key not in seen:  # a repeated draw would only re-measure the same recipe
            seen.add(key)
            produced += 1
            yield recipe


def _candidates(space, budget):
    """(recipes in evaluation order, the space's size when it is finite and known)."""
    params, mode = _parameters(space), space.get("mode")
    if mode == "grid":
        size = 1
        for _, spec in params:
            size *= len(spec.get("values") or [])
        return _grid(params), size
    if mode == "random":
        if "seed" not in space:
            raise SweepConfigError("random search needs space.seed, so the same recipes are drawn again")
        return _random(params, int(space["seed"]), space.get("samples", budget.get("max_trials"))), None
    raise SweepConfigError("space.mode must be grid or random")


# -- one evaluation -------------------------------------------------------------------------------------------------
def _format(value):
    return value if isinstance(value, str) else json.dumps(value)


def _render(template, parameters):
    """A recipe template with every {"$param": name} replaced by that parameter's value."""
    if isinstance(template, dict):
        if set(template) == {"$param"}:
            if template["$param"] not in parameters:
                raise SweepConfigError("recipe template names unknown parameter %r" % template["$param"])
            return parameters[template["$param"]]
        return {k: _render(v, parameters) for k, v in template.items()}
    if isinstance(template, list):
        return [_render(v, parameters) for v in template]
    return template


def _argv(command, substitutions):
    argv = []
    for i, arg in enumerate(command["argv"]):
        if i == 0 and arg == "python":
            arg = sys.executable
        for key, value in substitutions.items():
            arg = arg.replace(key, value)
        unknown = _PLACEHOLDER.findall(arg)
        if unknown:
            raise SweepConfigError("argv[%d] has unknown placeholders %s" % (i, unknown))
        argv.append(arg)
    return argv


def _prepare_work(work, inputs, reader, command, source_files):
    work.mkdir(parents=True)
    if source_files is not None:
        for rel, text in source_files.items():
            target = (work / rel).resolve()
            if work.resolve() not in target.parents:
                raise SweepConfigError("source bundle path escapes the working directory: %r" % rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
    else:
        allow = command.get("copy")
        for p in sorted(Path.cwd().iterdir()):
            if not p.is_file() or p.is_symlink() or p.stat().st_size > MAX_COPY_BYTES:
                continue
            if (allow is not None and p.name not in allow) or (allow is None and p.suffix in (".npz", ".pt", ".pth")):
                continue
            shutil.copyfile(p, work / p.name)
    names = dict(command.get("files") or {})
    for port, binding in inputs.items():
        if port == "source":
            continue
        name = names.get(port) or port + SUFFIX_BY_MEDIA.get(getattr(binding, "media_type", None) or "", "")
        if "/" in name or name.startswith("."):
            raise SweepConfigError("file name for %s must be a plain name" % port)
        shutil.copyfile(reader.path(port), work / name)


def _metrics(out_dir, metrics_file):
    path = out_dir / metrics_file
    if not path.is_file():
        raise ValueError("the command wrote no %s under {out}" % metrics_file)
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("%s is not a JSON object" % metrics_file)
    metrics, nonfinite = {}, []
    for key, value in data.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        try:
            number = float(value)
        except OverflowError:
            number = math.inf
        if math.isfinite(number):
            metrics[key] = number
        else:  # strict JSON has no NaN or infinity: a diverged metric is named, and an objective among them fails
            nonfinite.append(key)
    return metrics, sorted(nonfinite)


def _evaluate(root, index, seed, fold, n_folds, parameters, cfg, inputs, reader, source_files, timeout):
    """One recipe x seed x fold. Returns its record; a failure of the project's command is data, not a sweep failure."""
    command = cfg["command"]
    base = root / ("trial-%04d-s%s-f%d" % (index, seed, fold))
    work, out = base / "work", base / "out"
    _prepare_work(work, inputs, reader, command, source_files)
    out.mkdir()
    subst = {"{work}": str(work), "{out}": str(out), "{seed}": str(seed), "{fold}": str(fold),
             "{folds}": str(n_folds), "{params_json}": json.dumps(parameters, sort_keys=True)}
    subst.update({"{param.%s}" % k: _format(v) for k, v in parameters.items()})
    if cfg.get("recipe") is not None:
        recipe_path = work / "recipe.json"
        recipe_path.write_text(json.dumps(_render(cfg["recipe"], parameters), sort_keys=True))
        subst["{recipe}"] = str(recipe_path)
    argv = _argv(command, subst)
    record = {"seed": seed, "fold": fold}
    started = time.time()
    try:
        proc = subprocess.run(argv, cwd=str(work), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        record.update(status="timeout", error="stopped after %.1f s" % timeout, seconds=round(time.time() - started, 3))
        return record
    record["seconds"] = round(time.time() - started, 3)
    if proc.returncode != 0:
        record.update(status="failed", error="exit %d: %s" % (proc.returncode, proc.stderr[-TAIL_CHARS:]))
        return record
    try:
        metrics, nonfinite = _metrics(out, command.get("metrics_file", "receipt.json"))
    except ValueError as exc:
        record.update(status="failed", error=str(exc)[:TAIL_CHARS])
        return record
    if nonfinite:
        record["nonfinite_metrics"] = nonfinite
    heldout = sorted(k for k in [*metrics, *nonfinite] if _names_heldout(k))
    if heldout:  # visible validation only: a report of held-out performance is refused, not ranked
        record.update(status="failed", error="the command reported held-out metrics %s; this search uses visible "
                                             "validation only" % heldout)
        return record
    objective = cfg["objective"]["metric"]
    if objective not in metrics:
        record.update(status="failed", metrics=metrics, error="objective metric %r is missing or not finite" % objective)
        return record
    record.update(status="ok", metrics=metrics)
    return record


# -- summarising ----------------------------------------------------------------------------------------------------
def _summary(values):
    return {"mean": statistics.fmean(values), "std": statistics.stdev(values) if len(values) > 1 else 0.0, "n": len(values)}


def _recipe_row(index, parameters, evaluations, objective_metric):
    canonical = json.dumps(parameters, sort_keys=True)
    row = {"recipe_id": "r%04d-%s" % (index, hashlib.sha256(canonical.encode()).hexdigest()[:8]), "index": index,
           "parameters": parameters, "evaluations": evaluations, "rank": None, "objective": None, "metrics": {}}
    if any(e["status"] != "ok" for e in evaluations) or not evaluations:
        row["status"] = "failed"
        row["error"] = next((e.get("error") for e in evaluations if e["status"] != "ok"), "no evaluation ran")
        return row
    row["status"] = "ok"
    names = sorted(set.intersection(*[set(e["metrics"]) for e in evaluations]))
    row["metrics"] = {n: _summary([e["metrics"][n] for e in evaluations]) for n in names}
    row["objective"] = dict(row["metrics"][objective_metric], metric=objective_metric)
    return row


def _improves(value, best, direction, min_delta):
    if best is None:
        return True
    return value > best + min_delta if direction == "maximize" else value < best - min_delta


def _reached(value, target, direction):
    return value >= target if direction == "maximize" else value <= target


def _ranked(rows, direction):
    sign = -1.0 if direction == "maximize" else 1.0
    ok = sorted((r for r in rows if r["status"] == "ok"),
                key=lambda r: (sign * r["objective"]["mean"], r["objective"]["std"], r["index"]))
    for rank, row in enumerate(ok, 1):
        row["rank"] = rank
    return ok + [r for r in rows if r["status"] != "ok"]


def _evidence_digest(rows):
    """Stable over re-runs of a deterministic project: everything recorded except wall seconds."""
    stable = [{**r, "evaluations": [{k: v for k, v in e.items() if k != "seconds"} for e in r["evaluations"]]}
              for r in rows]
    return "sha256:" + hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def _budget(config, context):
    budget = dict(config.get("budget") or {})
    max_trials, max_seconds = budget.get("max_trials"), budget.get("max_seconds")
    if max_trials is None and max_seconds is None:
        raise SweepConfigError("budget needs max_trials or max_seconds: an unbounded search is refused")
    wall = (context.limits or {}).get("wall_seconds")
    if wall is not None:  # the job's own wall limit bounds the search, with room to write the table
        ceiling = max(1.0, float(wall) * 0.9)
        budget["max_seconds"] = min(float(max_seconds), ceiling) if max_seconds is not None else ceiling
    return budget


def _validated(config):
    objective = config.get("objective") or {}
    if not objective.get("metric"):
        raise SweepConfigError("objective.metric is required (the visible-validation metric the command reports)")
    if _names_heldout(objective["metric"]):
        raise SweepConfigError("objective %r names a held-out split; this search uses visible validation only"
                               % objective["metric"])
    if objective.get("direction", "maximize") not in ("maximize", "minimize"):
        raise SweepConfigError("objective.direction must be maximize or minimize")
    command = config.get("command") or {}
    if not isinstance(command.get("argv"), list) or len(command["argv"]) < 2:
        raise SweepConfigError("command.argv must be an argument array with at least two items")
    stopping = dict(config.get("stopping") or {"rule": "none"})
    if stopping.get("rule", "none") not in ("none", "patience", "target"):
        raise SweepConfigError("stopping.rule must be none, patience or target")
    if stopping.get("rule") == "patience" and int(stopping.get("patience", 0)) < 1:
        raise SweepConfigError("stopping.patience must be at least 1")
    if stopping.get("rule") == "target" and "target" not in stopping:
        raise SweepConfigError("stopping.target is required by the target rule")
    return dict(objective, direction=objective.get("direction", "maximize")), stopping


def _seeds_folds(config, context):
    policy = dict(config.get("seed_policy") or {})
    seeds = policy.get("seeds")
    if seeds is None:
        seeds = [context.seed if context.seed is not None else 0]
    if not isinstance(seeds, list) or not seeds or not all(isinstance(s, int) and not isinstance(s, bool) for s in seeds):
        raise SweepConfigError("seed_policy.seeds must be a non-empty list of integers")
    folds = int(policy.get("folds", 1))
    if folds < 1:
        raise SweepConfigError("seed_policy.folds must be at least 1")
    return seeds, folds


def run(inputs, config, *, reader, writer, context):
    extra = sorted(set(inputs) - set(ALLOWED_INPUTS))
    if extra:
        raise SweepConfigError("undeclared input ports %s: only %s are read" % (extra, list(ALLOWED_INPUTS)))
    objective, stopping = _validated(config)
    budget = _budget(config, context)
    candidates, space_size = _candidates(config.get("space") or {}, budget)
    seeds, folds = _seeds_folds(config, context)
    source_files = reader.json("source").get("files") if "source" in inputs else None
    root = Path(context.scratch_dir) / "sweep"
    root.mkdir(parents=True, exist_ok=True)
    direction, metric = objective["direction"], objective["metric"]
    max_trials, max_seconds = budget.get("max_trials"), budget.get("max_seconds")
    per_eval = float(config["command"].get("timeout_seconds", 600))
    started = time.time()
    rows, best, stale, stop_reason, index = [], None, 0, "space-exhausted", 0

    def remaining():
        return None if max_seconds is None else float(max_seconds) - (time.time() - started)

    for parameters in candidates:
        if max_trials is not None and index >= int(max_trials):
            stop_reason = "budget-trials"
            break
        left = remaining()
        if left is not None and left <= 0:
            stop_reason = "budget-seconds"
            break
        evaluations, timed_out = [], False
        for seed, fold in itertools.product(seeds, range(folds)):
            left = remaining()
            if left is not None and left <= 0:
                timed_out = True
                break
            timeout = per_eval if left is None else min(per_eval, left)
            evaluations.append(_evaluate(root, index, seed, fold, folds, parameters, config, inputs, reader,
                                         source_files, timeout))
        if timed_out and not evaluations:
            stop_reason = "budget-seconds"
            break
        row = _recipe_row(index, parameters, evaluations, metric)
        if timed_out:  # a partial recipe is recorded, and never ranked: its seeds and folds were not all measured
            row.update(status="failed", error="budget ended before every seed and fold ran", rank=None, objective=None,
                       metrics={})
        rows.append(row)
        index += 1
        if timed_out:
            stop_reason = "budget-seconds"
            break
        value = row["objective"]["mean"] if row["status"] == "ok" else None
        if value is not None and _improves(value, best, direction, float(stopping.get("min_delta", 0.0))):
            best, stale = value, 0
        else:
            stale += 1
        if value is not None and stopping.get("rule") == "target" and _reached(value, float(stopping["target"]), direction):
            stop_reason = "stopping-target"
            break
        if stopping.get("rule") == "patience" and stale >= int(stopping["patience"]):
            stop_reason = "stopping-patience"
            break
    else:
        if max_trials is not None and index >= int(max_trials) and (space_size is None or index < space_size):
            stop_reason = "budget-trials"
    ranked = _ranked(rows, direction)
    usable = [r for r in ranked if r["status"] == "ok"]
    winner = usable[0] if usable else None
    disclaimer = ("Exploratory selection on visible validation. No claim is made and no test data was read. Lock the "
                  "winner before independent confirmation.")
    table = {"schema": TABLE_SCHEMA, "exploratory": True, "claim": None, "selection_split": "visible-validation",
             "disclaimer": disclaimer, "objective": objective, "space": config.get("space"),
             "seed_policy": {"seeds": seeds, "folds": folds}, "budget": budget, "stopping": stopping,
             "stop_reason": stop_reason, "seconds": round(time.time() - started, 3),
             "counts": {"space_size": space_size, "evaluated": len(rows), "usable": len(usable),
                        "failed": len(rows) - len(usable)},
             "recipes": ranked}
    best_doc = {"schema": BEST_SCHEMA, "status": "found" if winner else "none", "exploratory": True, "claim": None,
                "selection_split": "visible-validation", "lock_required": True, "disclaimer": disclaimer,
                "objective": objective, "recipe": None if winner is None else {
                    "recipe_id": winner["recipe_id"], "parameters": winner["parameters"],
                    "objective": winner["objective"], "metrics": winner["metrics"],
                    "evaluations": winner["evaluations"], "command": config["command"],
                    "recipe_template": config.get("recipe")}}
    writer.stage_json("evidence_table", table, schema=TABLE_SCHEMA)
    writer.stage_json("best_recipe", best_doc, schema=BEST_SCHEMA)
    digest = _evidence_digest(ranked)
    receipt_metrics = {"recipes_evaluated": len(rows), "recipes_usable": len(usable), "recipes_failed": len(rows) - len(usable)}
    if winner is not None:
        receipt_metrics["best_objective"] = winner["objective"]["mean"]
    context.record("stop_reason", stop_reason)
    context.record("evidence_digest", digest)
    return {"metrics": receipt_metrics, "stop_reason": stop_reason, "evidence_digest": digest,
            "best_recipe_id": winner["recipe_id"] if winner else None, "exploratory": True}
