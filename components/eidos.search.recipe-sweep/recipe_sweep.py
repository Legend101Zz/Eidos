"""General recipe search over a project command (standard library only, Python 3.9+).

Runs one command per recipe x seed x fold in a private working directory, using visible training and validation data
only, and writes a ranked evidence table with every evaluated recipe and the best one. Exploratory: no claim and no test
data. Lock the winner before independent confirmation.
"""
import hashlib
import itertools
import json
import math
import os
import random
import re
import shutil
import signal
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
TAIL_READ_BYTES = 64 << 10
# An evaluation that outlives its time gets this long after SIGTERM before SIGKILL (below the launcher's own grace).
GROUP_GRACE_SECONDS = 2.0
GROUP_KILL_WAIT_SECONDS = 3.0
# What an evaluation inherits; everything else (provider keys, cloud credentials, lab tokens) is dropped. HOME and TMPDIR
# are its own directories, so a tool's default path does not reach the real home directory's credentials.
ENV_ALLOW = ("PATH", "LANG", "TZ", "PYTHONHASHSEED", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
             "CUDA_VISIBLE_DEVICES", "PYTORCH_ENABLE_MPS_FALLBACK", "PYTORCH_MPS_HIGH_WATERMARK_RATIO")
ENV_ALLOW_PREFIXES = ("LC_",)
MIN_SECRET_VALUE_CHARS = 8
REDACTED = "[REDACTED]"
_SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(authorization\s*:\s*)?(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}"), REDACTED),
    (re.compile(r"(?i)([A-Za-z0-9_.\-]*(?:api[_\-]?key|access[_\-]?key|secret|token|passw(?:or)?d|credential)"
                r"[A-Za-z0-9_.\-]*[\"']?\s*[:=]\s*)[\"']?[^\s\"',;]+"), r"\1" + REDACTED),
    (re.compile(r"\b(?:sk|pk|rk|ghp|gho|ghs|ghu|github_pat|glpat|hf|xox[abprs])[-_][A-Za-z0-9_\-]{8,}"), REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]*"), REDACTED),
    (re.compile(r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-]{32,}(?![A-Za-z0-9_\-])"), REDACTED),
    (re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}={1,2}"), REDACTED),
)
# A source-bundle file may be named after the held-out split only if it is code; data under such a name is refused.
SOURCE_CODE_SUFFIXES = (".py", ".sh", ".md", ".toml")
_PARAM = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_.]*\}")
_HELDOUT = re.compile(r"(^|[^a-z0-9])(test(?:set|data|split)?|hold[_\- ]?out|held[_\- ]?out)([^a-z0-9]|$)")
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


def _check_project_files(command, source_files):
    """What an evaluation's working directory may hold besides the ports: a source bundle, or an explicit list of project
    files. Nothing is copied implicitly, and neither route can bring in a file named after the held-out split."""
    if source_files is not None:
        for rel in source_files:
            parts = str(rel).replace("\\", "/").split("/")
            if any(_names_heldout(part) for part in parts) and Path(parts[-1]).suffix not in SOURCE_CODE_SUFFIXES:
                raise SweepConfigError("source bundle file %r is named after a held-out split; this search uses visible "
                                       "validation only" % rel)
        return
    allow = command.get("copy")
    if not isinstance(allow, list) or not allow:
        raise SweepConfigError("without a source bundle, command.copy must list the project files to copy (plain file "
                               "names): nothing is copied implicitly, so held-out data cannot reach an evaluation")
    for name in allow:
        if not isinstance(name, str) or not name or "/" in name or "\\" in name or name.startswith("."):
            raise SweepConfigError("command.copy entry %r must be a plain file name" % (name,))
        if _names_heldout(name):
            raise SweepConfigError("command.copy entry %r is named after a held-out split; this search uses visible "
                                   "validation only" % name)
        path = Path.cwd() / name
        if path.is_symlink() or not path.is_file():
            raise SweepConfigError("command.copy entry %r is not a regular file in the project directory" % name)
        if path.stat().st_size > MAX_COPY_BYTES:
            raise SweepConfigError("command.copy entry %r is larger than %d bytes; pass data through a port" % (name, MAX_COPY_BYTES))


def _prepare_work(work, inputs, reader, command, source_files):
    work.mkdir(parents=True)
    if source_files is not None:
        for rel, text in source_files.items():
            target = (work / rel).resolve()
            if work.resolve() not in target.parents:
                raise SweepConfigError("source bundle path escapes the working directory: %r" % rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
    else:  # _check_project_files has vetted these names
        for name in command["copy"]:
            shutil.copyfile(Path.cwd() / name, work / name)
    names = dict(command.get("files") or {})
    for port, binding in inputs.items():
        if port == "source":
            continue
        name = names.get(port) or port + SUFFIX_BY_MEDIA.get(getattr(binding, "media_type", None) or "", "")
        if "/" in name or name.startswith("."):
            raise SweepConfigError("file name for %s must be a plain name" % port)
        shutil.copyfile(reader.path(port), work / name)


# -- running a command: a scrubbed environment, its own process group, redacted output -------------------------------
def _scrubbed_env(home, tmp):
    env = {k: v for k, v in os.environ.items() if k in ENV_ALLOW or k.startswith(ENV_ALLOW_PREFIXES)}
    env.update(HOME=str(home), TMPDIR=str(tmp))
    return env


def _secret_values():
    """Values of the variables an evaluation does not inherit, longest first: none of them may appear in published text."""
    values = {v for k, v in os.environ.items()
              if not (k in ENV_ALLOW or k.startswith(ENV_ALLOW_PREFIXES)) and len(v) >= MIN_SECRET_VALUE_CHARS}
    return sorted(values, key=len, reverse=True)


def _redact(text, secrets):
    for value in secrets:
        text = text.replace(value, REDACTED)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _stderr_tail(path, secrets):
    """The end of a command's stderr, redacted BEFORE it is cut so a secret cannot survive as a fragment."""
    with open(str(path), "rb") as fh:
        fh.seek(max(0, os.fstat(fh.fileno()).st_size - TAIL_READ_BYTES))
        text = fh.read().decode("utf-8", "replace")
    return _redact(text, secrets)[-TAIL_CHARS:]


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):  # PermissionError: macOS, when only zombies are left
        return False
    return True


def _signal_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _group_gone(proc, within):
    deadline = time.monotonic() + within
    while True:
        proc.poll()  # reaps the group's leader, which would otherwise count as a member until it is waited for
        if not _group_alive(proc.pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _stop_group(proc):
    """SIGTERM the whole process group, SIGKILL what outlives the grace period, reap the leader and wait until no member
    is left. True when the group is gone (a command's workers can then not overlap the next evaluation)."""
    if _group_gone(proc, 0):
        return True
    _signal_group(proc.pid, signal.SIGTERM)
    if _group_gone(proc, GROUP_GRACE_SECONDS):
        return True
    _signal_group(proc.pid, signal.SIGKILL)
    return _group_gone(proc, GROUP_KILL_WAIT_SECONDS)


_active_evaluation = None  # the running command's Popen, for the SIGTERM handler


def _on_sigterm(signum, frame):
    """The launcher cancels a job with SIGTERM to the worker's process group, which an evaluation has left."""
    if _active_evaluation is not None:
        _stop_group(_active_evaluation)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTERM)


def _run_group(argv, work, env, stderr_path, timeout, stop_requested):
    """Run ``argv`` as the leader of a new process group. Returns (``exited``, ``timeout`` or ``cancelled``, exit code,
    whether the group is gone); on every path, a normal exit included, the group has been stopped when it returns. Only a
    member stuck in the kernel, or a zombie nothing reaps (a container without an init), can outlive SIGKILL."""
    global _active_evaluation
    deadline = time.monotonic() + timeout
    with open(str(stderr_path), "wb") as err:  # a file, not a pipe: a straggler holding it open cannot block the wait
        proc = subprocess.Popen(argv, cwd=str(work), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=err, start_new_session=True)
    _active_evaluation = proc
    outcome = "exited"
    try:
        while proc.poll() is None:
            if stop_requested():
                outcome = "cancelled"
                break
            if time.monotonic() >= deadline:
                outcome = "timeout"
                break
            time.sleep(0.02)
    finally:
        gone = _stop_group(proc)
        _active_evaluation = None
    return outcome, proc.returncode, gone


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


def _evaluate(root, index, seed, fold, n_folds, parameters, cfg, inputs, reader, source_files, timeout, secrets, stop):
    """One recipe x seed x fold. Returns its record; a failure of the project's command is data, not a sweep failure."""
    command = cfg["command"]
    label = index if isinstance(index, str) else "%04d" % index  # a template's stages prefix their own labels
    base = root / ("trial-%s-s%s-f%d" % (label, seed, fold))
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
    home = base / "home"
    home.mkdir()
    stderr_path = base / "stderr.log"
    started = time.time()
    outcome, returncode, gone = _run_group(argv, work, _scrubbed_env(home, home), stderr_path, timeout, stop)
    record["seconds"] = round(time.time() - started, 3)
    if not gone:
        record["process_group_lingering"] = True
    if outcome == "timeout":
        record.update(status="timeout", error="stopped after %.1f s" % timeout)
        return record
    if outcome == "cancelled":
        record.update(status="cancelled", error="stopped: the job was asked to stop")
        return record
    if returncode != 0:
        record.update(status="failed", error="exit %d: %s" % (returncode, _stderr_tail(stderr_path, secrets)))
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


def _search(candidates, space_size, cfg, *, inputs, reader, source_files, seeds, folds, budget, stopping, context, root,
            secrets, started, first_index=0, tag=None):
    """Evaluate recipes in order until the space, the budget, the stopping rule or a stop request ends the search.
    Returns (rows, stop_reason). ``started`` is the wall-clock start that ``budget.max_seconds`` counts from."""
    direction, metric = cfg["objective"].get("direction", "maximize"), cfg["objective"]["metric"]
    max_trials, max_seconds = budget.get("max_trials"), budget.get("max_seconds")
    per_eval = float(cfg["command"].get("timeout_seconds", 600))
    rows, best, stale, stop_reason, count = [], None, 0, "space-exhausted", 0

    def remaining():
        return None if max_seconds is None else float(max_seconds) - (time.time() - started)

    for parameters in candidates:
        index = first_index + count
        if max_trials is not None and count >= int(max_trials):
            stop_reason = "budget-trials"
            break
        left = remaining()
        if left is not None and left <= 0:
            stop_reason = "budget-seconds"
            break
        if context.stop_requested():
            stop_reason = "stop-requested"
            break
        evaluations, timed_out = [], False
        for seed, fold in itertools.product(seeds, range(folds)):
            left = remaining()
            if left is not None and left <= 0:
                timed_out = True
                break
            timeout = per_eval if left is None else min(per_eval, left)
            evaluations.append(_evaluate(root, index if tag is None else "%s-%04d" % (tag, index), seed, fold, folds,
                                         parameters, cfg, inputs, reader, source_files, timeout, secrets,
                                         context.stop_requested))
            if evaluations[-1]["status"] == "cancelled":
                break
        if timed_out and not evaluations:
            stop_reason = "budget-seconds"
            break
        row = _recipe_row(index, parameters, evaluations, metric)
        if timed_out:  # a partial recipe is recorded, and never ranked: its seeds and folds were not all measured
            row.update(status="failed", error="budget ended before every seed and fold ran", rank=None, objective=None,
                       metrics={})
        rows.append(row)
        count += 1
        if any(e["status"] == "cancelled" for e in evaluations):
            stop_reason = "stop-requested"
            break
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
        if max_trials is not None and count >= int(max_trials) and (space_size is None or count < space_size):
            stop_reason = "budget-trials"
    return rows, stop_reason


def _install_sigterm():
    try:
        return signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:  # not the main thread: only the runner's cooperative stop can reach this search
        return None


DISCLAIMER = ("Exploratory selection on visible validation. No claim is made and no test data was given to the "
              "evaluations. Lock the winner before independent confirmation.")


def _write(writer, context, table, best_doc, ranked, receipt_extra=None):
    usable = [r for r in ranked if r["status"] == "ok"]
    winner_id = (best_doc.get("recipe") or {}).get("recipe_id")
    writer.stage_json("evidence_table", table, schema=TABLE_SCHEMA)
    writer.stage_json("best_recipe", best_doc, schema=BEST_SCHEMA)
    digest = _evidence_digest(ranked)
    receipt_metrics = {"recipes_evaluated": len(ranked), "recipes_usable": len(usable),
                       "recipes_failed": len(ranked) - len(usable)}
    if best_doc.get("recipe"):
        receipt_metrics["best_objective"] = best_doc["recipe"]["objective"]["mean"]
    context.record("stop_reason", table["stop_reason"])
    context.record("evidence_digest", digest)
    return {"metrics": receipt_metrics, "stop_reason": table["stop_reason"], "evidence_digest": digest,
            "best_recipe_id": winner_id, "exploratory": True, **(receipt_extra or {})}


def run(inputs, config, *, reader, writer, context):
    extra = sorted(set(inputs) - set(ALLOWED_INPUTS))
    if extra:
        raise SweepConfigError("undeclared input ports %s: only %s are read" % (extra, list(ALLOWED_INPUTS)))
    if "template" in config:
        return _run_template(inputs, config, reader=reader, writer=writer, context=context)
    objective, stopping = _validated(config)
    budget = _budget(config, context)
    candidates, space_size = _candidates(config.get("space") or {}, budget)
    seeds, folds = _seeds_folds(config, context)
    source_files = reader.json("source").get("files") if "source" in inputs else None
    _check_project_files(config["command"], source_files)
    secrets = _secret_values()
    root = Path(context.scratch_dir) / "sweep"
    root.mkdir(parents=True, exist_ok=True)
    direction = objective["direction"]
    previous_sigterm = _install_sigterm()
    started = time.time()
    try:
        rows, stop_reason = _search(candidates, space_size, dict(config, objective=objective), inputs=inputs,
                                    reader=reader, source_files=source_files, seeds=seeds, folds=folds, budget=budget,
                                    stopping=stopping, context=context, root=root, secrets=secrets, started=started)
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
    ranked = _ranked(rows, direction)
    usable = [r for r in ranked if r["status"] == "ok"]
    winner = usable[0] if usable else None
    table = {"schema": TABLE_SCHEMA, "exploratory": True, "claim": None, "selection_split": "visible-validation",
             "disclaimer": DISCLAIMER, "objective": objective, "space": config.get("space"),
             "seed_policy": {"seeds": seeds, "folds": folds}, "budget": budget, "stopping": stopping,
             "stop_reason": stop_reason, "seconds": round(time.time() - started, 3),
             "counts": {"space_size": space_size, "evaluated": len(rows), "usable": len(usable),
                        "failed": len(rows) - len(usable)},
             "recipes": ranked}
    best_doc = {"schema": BEST_SCHEMA, "status": "found" if winner else "none", "exploratory": True, "claim": None,
                "selection_split": "visible-validation", "lock_required": True, "disclaimer": DISCLAIMER,
                "objective": objective, "recipe": None if winner is None else {
                    "recipe_id": winner["recipe_id"], "parameters": winner["parameters"],
                    "objective": winner["objective"], "metrics": winner["metrics"],
                    "evaluations": winner["evaluations"], "command": config["command"],
                    "recipe_template": config.get("recipe")}}
    return _write(writer, context, table, best_doc, ranked)


# -- typed templates: preprocessing x model family, screened on validation, finalists cross-validated within train ----
TEMPLATE_RUNNER = "fit_recipe.py"
TEMPLATE_PREPROCESS = ("none", "standardize", "per_example_center", "per_example_standardize")
TEMPLATE_FAMILIES = ("linear", "knn", "kernel", "mlp", "svm", "gbm")
SKLEARN_FAMILIES = ("svm", "gbm")
# Settings per family: "quick" is one setting each; "broad" adds a second regularization or size.
TEMPLATE_SETTINGS = {
    "quick": {"linear": [{"alpha": 1e-3}], "knn": [{"k": 5}], "kernel": [{"alpha": 1e-2}], "mlp": [{"hidden": 64}],
              "svm": [{"C": 3.0}], "gbm": [{"lr": 0.1}]},
    "broad": {"linear": [{"alpha": 1e-4}, {"alpha": 1e-2}], "knn": [{"k": 1}, {"k": 5}, {"k": 15}],
              "kernel": [{"alpha": 1e-3}, {"alpha": 1e-1}], "mlp": [{"hidden": 64}, {"hidden": 256}],
              "svm": [{"C": 1.0}, {"C": 10.0}], "gbm": [{"lr": 0.1}, {"lr": 0.03, "max_iter": 400}]},
}
TEMPLATE_KEYS = ("name", "task_type", "preprocess", "families", "finalists", "cv_folds", "seeds", "rank_by",
                 "timeout_seconds", "max_rows")
TEMPLATE_METRIC = {"classification": ("accuracy", "maximize"), "regression": ("rmse", "minimize")}


def _sklearn_available():
    import importlib.util
    return importlib.util.find_spec("sklearn") is not None


def _template_spec(inputs, config, reader, context):
    """The typed template, checked: (template, task_type, recipes, skipped families)."""
    template = config["template"]
    if not isinstance(template, dict):
        raise SweepConfigError("template must be an object with at least a name")
    unknown = sorted(set(template) - set(TEMPLATE_KEYS))
    if unknown:
        raise SweepConfigError("unknown template keys %s; allowed %s" % (unknown, list(TEMPLATE_KEYS)))
    clash = sorted(k for k in ("command", "space", "objective", "recipe") if k in config)
    if clash:
        raise SweepConfigError("a template builds its own command, space and objective; remove %s" % clash)
    if "source" in inputs:
        raise SweepConfigError("a template runs its own recipe runner: give train and validation, not a source bundle")
    for port in ("train", "validation"):
        if port not in inputs:
            raise SweepConfigError("a template needs the %s port (an NPZ with x and y)" % port)
    name = template.get("name")
    if name not in TEMPLATE_SETTINGS:
        raise SweepConfigError("template.name must be one of %s" % sorted(TEMPLATE_SETTINGS))
    task_type = template.get("task_type") or (reader.json("task").get("task_type") if "task" in inputs else None)
    if task_type not in TEMPLATE_METRIC:
        raise SweepConfigError("template.task_type (or the task input's task_type) must be classification or regression")
    preprocess = list(template.get("preprocess") or TEMPLATE_PREPROCESS)
    families = list(template.get("families") or TEMPLATE_FAMILIES)
    bad = sorted(set(preprocess) - set(TEMPLATE_PREPROCESS)) + sorted(set(families) - set(TEMPLATE_FAMILIES))
    if bad or not preprocess or not families:
        raise SweepConfigError("template.preprocess must be drawn from %s and template.families from %s; unknown %s"
                               % (list(TEMPLATE_PREPROCESS), list(TEMPLATE_FAMILIES), bad))
    if template.get("rank_by", "validation") not in ("validation", "train-cv"):
        raise SweepConfigError("template.rank_by must be validation or train-cv")
    if int(template.get("finalists", 3)) < 0 or int(template.get("cv_folds", 3)) < 2:
        raise SweepConfigError("template.finalists must be >= 0 and template.cv_folds >= 2")
    skipped = {}
    if not _sklearn_available():
        for family in SKLEARN_FAMILIES:
            if family in families:
                families.remove(family)
                skipped[family] = "scikit-learn is not importable in this environment"
    if not families:
        raise SweepConfigError("no template family is available in this environment: %s" % skipped)
    recipes = []
    for family in families:  # family-major order: a trials budget still sees every preprocessing of the first families
        for settings in TEMPLATE_SETTINGS[name][family]:
            for pre in preprocess:
                settings_used = dict(settings)
                if template.get("max_rows") is not None and family in ("kernel", "svm", "gbm"):
                    settings_used["max_rows"] = int(template["max_rows"])
                recipes.append({"preprocess": pre, "family": family, "settings": settings_used})
    return template, task_type, recipes, skipped


def _template_command(task_type, mode, timeout):
    argv = ["python", TEMPLATE_RUNNER, mode, "--out", "{out}", "--seed", "{seed}", "--params", "{params_json}",
            "--task-type", task_type]
    if mode == "cv":
        argv += ["--fold", "{fold}", "--folds", "{folds}"]
    return {"argv": argv, "files": {"train": "train.npz", "validation": "validation.npz", "task": "task.json"},
            "timeout_seconds": timeout}


def _run_template(inputs, config, *, reader, writer, context):
    """One call, no authored glue: every preprocessing x family recipe is screened (fit on train, scored on visible
    validation, every seed), then the best ``finalists`` are cross-validated within train. Every attempted recipe is
    recorded; the best is chosen by ``rank_by`` among the finalists (validation by default)."""
    template, task_type, recipes, skipped = _template_spec(inputs, config, reader, context)
    metric, direction = TEMPLATE_METRIC[task_type]
    budget = _budget(dict(config, budget=config.get("budget") or {"max_trials": len(recipes)}), context)
    seeds = template.get("seeds") or [context.seed if context.seed is not None else 0]
    if not isinstance(seeds, list) or not all(isinstance(s, int) and not isinstance(s, bool) for s in seeds):
        raise SweepConfigError("template.seeds must be a non-empty list of integers")
    stopping = dict(config.get("stopping") or {"rule": "none"})
    if stopping.get("rule", "none") != "none":
        raise SweepConfigError("a template screens every recipe it can within the budget: stopping.rule must be none")
    timeout = float(template.get("timeout_seconds", 300))
    runner = Path(__file__).resolve().parent / "templates" / TEMPLATE_RUNNER
    source_files = {TEMPLATE_RUNNER: runner.read_text()}
    secrets = _secret_values()
    root = Path(context.scratch_dir) / "sweep"
    root.mkdir(parents=True, exist_ok=True)
    screen_cfg = {"command": _template_command(task_type, "screen", timeout),
                  "objective": {"metric": "validation_" + metric, "direction": direction}}
    cv_metric = "train_cv_" + metric
    cv_cfg = {"command": _template_command(task_type, "cv", timeout),
              "objective": {"metric": cv_metric, "direction": direction}}
    folds, n_finalists = int(template.get("cv_folds", 3)), int(template.get("finalists", 3))
    previous_sigterm = _install_sigterm()
    started = time.time()
    try:
        rows, stop_reason = _search(iter(recipes), len(recipes), screen_cfg, inputs=inputs, reader=reader,
                                    source_files=source_files, seeds=seeds, folds=1, budget=budget, stopping=stopping,
                                    context=context, root=root, secrets=secrets, started=started, tag="screen")
        screened = _ranked(rows, direction)
        finalists = [r for r in screened if r["status"] == "ok"][:n_finalists]
        cv_stop = "not-run"
        if finalists and stop_reason in ("space-exhausted", "budget-trials"):
            cv_budget = {k: v for k, v in budget.items() if k == "max_seconds"}
            cv_budget["max_trials"] = len(finalists)
            cv_rows, cv_stop = _search(iter([r["parameters"] for r in finalists]), len(finalists), cv_cfg,
                                       inputs=inputs, reader=reader, source_files=source_files, seeds=seeds,
                                       folds=folds, budget=cv_budget, stopping={"rule": "none"}, context=context,
                                       root=root, secrets=secrets, started=started, tag="cv")
            for finalist, cv_row in zip(finalists, cv_rows):
                finalist["train_cv"] = {k: cv_row[k] for k in ("status", "objective", "metrics", "evaluations")}
                if cv_row.get("error"):
                    finalist["train_cv"]["error"] = cv_row["error"]
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
    for row in screened:
        row["stage"] = "finalist" if "train_cv" in row else "screened"
    rank_by = template.get("rank_by", "validation")
    sign = -1.0 if direction == "maximize" else 1.0
    complete = [r for r in finalists if r.get("train_cv", {}).get("status") == "ok"]
    if rank_by == "train-cv":
        ordered = sorted(complete, key=lambda r: (sign * r["train_cv"]["objective"]["mean"], r["rank"]))
    else:
        ordered = sorted(complete, key=lambda r: r["rank"])
    for position, row in enumerate(ordered, 1):
        row["finalist_rank"] = position
        cv_mean, val_mean = row["train_cv"]["objective"]["mean"], row["objective"]["mean"]
        row["transfer_gap"] = (cv_mean - val_mean) if direction == "maximize" else (val_mean - cv_mean)
    winner = ordered[0] if ordered else None
    usable = [r for r in screened if r["status"] == "ok"]
    resolved = {"name": template["name"], "task_type": task_type, "preprocess": sorted({r["preprocess"] for r in recipes}),
                "families": sorted({r["family"] for r in recipes}), "skipped_families": skipped,
                "finalists": n_finalists, "cv_folds": folds, "seeds": seeds, "rank_by": rank_by,
                "runner": {"file": TEMPLATE_RUNNER, "sha256": hashlib.sha256(runner.read_bytes()).hexdigest()},
                "screen_command": screen_cfg["command"], "cv_command": cv_cfg["command"]}
    objective = dict(screen_cfg["objective"])
    table = {"schema": TABLE_SCHEMA, "exploratory": True, "claim": None, "selection_split": "visible-validation",
             "disclaimer": DISCLAIMER, "objective": objective, "template": resolved,
             "space": {"mode": "template", "recipes": len(recipes)},
             "seed_policy": {"seeds": seeds, "folds": 1, "finalist_cv_folds": folds}, "budget": budget,
             "stopping": stopping, "stop_reason": stop_reason, "finalist_stop_reason": cv_stop,
             "seconds": round(time.time() - started, 3),
             "counts": {"space_size": len(recipes), "evaluated": len(screened), "usable": len(usable),
                        "failed": len(screened) - len(usable), "finalists": len(finalists),
                        "finalists_cross_validated": len(complete)},
             "recipes": screened}
    best_doc = {"schema": BEST_SCHEMA, "status": "found" if winner else "none", "exploratory": True, "claim": None,
                "selection_split": "visible-validation", "lock_required": True, "disclaimer": DISCLAIMER,
                "objective": objective, "template": {k: resolved[k] for k in ("name", "task_type", "rank_by", "runner")},
                "recipe": None if winner is None else {
                    "recipe_id": winner["recipe_id"], "parameters": winner["parameters"],
                    "objective": winner["objective"], "metrics": winner["metrics"], "train_cv": winner["train_cv"],
                    "transfer_gap": winner["transfer_gap"], "evaluations": winner["evaluations"],
                    "command": screen_cfg["command"],
                    "reproduce": {"files": [TEMPLATE_RUNNER],
                                  "argv": ["python", TEMPLATE_RUNNER, "train", "--inputs", ".", "--out", "<out>",
                                           "--seed", str(seeds[0]), "--params",
                                           json.dumps(winner["parameters"], sort_keys=True), "--task-type", task_type],
                                  "note": "the runner is the evidence_table's template.runner; it reads train.npz and "
                                          "validation.npz from --inputs and writes validation_predictions.npz"}}}
    return _write(writer, context, table, best_doc, screened, {"template": template["name"], "task_type": task_type})
