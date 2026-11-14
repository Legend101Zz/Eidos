"""Resumable candidate intake, qualification and privacy-preserving pool construction."""

import argparse
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

from .common import read_json, sha, subprocess_env, write_json

PRIORITY = ("D", "E1", "H", "E2", "E3")
FINISHED = {"rejected", "qualified", "unqualified", "skipped_balance", "surplus_task"}


def parse_want(value):
    try:
        parts = [part.split("=") for part in value.split(",")]
        result = {name: int(size) for name, size in parts}
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("want must look like D=8,E1=8,H=12") from None
    if (len(result) != len(parts) or not result or any(n < 0 for n in result.values()) or
            any(p not in {*PRIORITY, "SMOKE"} for p in result) or ("SMOKE" in result and result != {"SMOKE": 2})):
        raise argparse.ArgumentTypeError("use nonnegative D/E/H sizes, or exactly SMOKE=2")
    return result


def _atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    write_json(temporary, value)
    temporary.replace(path)


def _safe_reason(error):
    # Never copy subprocess stderr (which can name a future H card) into public state/logs.
    if isinstance(error, ValueError):
        for reason in ("duplicate table content", "minority class fraction/imbalanced cap",
                       "fewer than 3 non-constant train features", "table cannot supply at least 600 train rows",
                       "missing", "nonfinite", "real table size/features", "classification requires",
                       "unsupported", "ragged table", "constant train target"):
            if reason in str(error):
                return reason
    return type(error).__name__


def _worker(action, request_path, response_path):
    os.nice(10)
    request = read_json(request_path)
    try:
        candidate = request["candidate"]
        if action == "preflight":
            from . import intake
            from .select import check_table

            source_dir = Path(request["cache"]) / "raw" / candidate["source"] / candidate["key"]
            if not (source_dir / "SOURCE.json").exists():
                # A partial failed download is not silently reused.
                if source_dir.exists():
                    shutil.rmtree(source_dir)
                source_dir = intake.intake(candidate["source"], candidate["key"], request["cache"],
                                           allow_root=request["allow_root"], target=candidate.get("target"))
            table = intake.load_table(source_dir)
            result = check_table(table, candidate["task_type"], seed=request["seed"])
            result.update(ok=True, source_dir=str(source_dir))
        else:
            from .package import package
            from .qualify import qualify
            from .reference import reference

            root = Path(request["root"])
            # Retrying an interrupted candidate never changes its allocated ID.
            for name in ("seal", "controller"):
                path = root / name / request["card_id"]
                if path.exists():
                    shutil.rmtree(path)
            card = package(request["source_dir"], request["card_id"], root,
                           candidate["task_type"], request["seed"])
            controller = root / "controller" / request["card_id"]
            measured = reference(card, controller, budget_seconds=request["budget_seconds"])
            qualification = qualify(card, controller, allow_null=True)
            result = dict(ok=True, qualified=qualification["qualified"], null_card=measured["null_card"],
                          failed_checks=[name for name, check in qualification["checks"].items() if not check["passed"]])
        write_json(response_path, result)
    except Exception as error:
        write_json(response_path, dict(ok=False, reason=_safe_reason(error), detail=str(error)))


def _launch(action, request, directory):
    directory.mkdir(parents=True, exist_ok=True)
    request_path, response = directory / f"{action}.request.json", directory / f"{action}.response.json"
    write_json(request_path, request)
    response.unlink(missing_ok=True)
    output = (directory / f"{action}.log").open("wb")
    try:
        process = subprocess.Popen([sys.executable, "-m", "study.exp.cards.build", "--worker-action", action,
                                    "--request", str(request_path), "--response", str(response)],
                                   stdout=output, stderr=subprocess.STDOUT, env=subprocess_env(), start_new_session=True)
    finally:
        output.close()
    return process, response


def _stop(process):
    # Workers own a process group, including their CV/retrain subprocesses.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def build(catalog_path, cache, want, *, workers=3, budget_seconds=480, private_root=None, resume=False,
          candidate_timeout=1500, allow_root=False, launch=None):
    from . import pools
    from .intake import CACHE_ROOT

    if workers < 1 or budget_seconds <= 0 or candidate_timeout <= 0:
        raise ValueError("workers, budget and candidate timeout must be positive")
    if want.get("H", 0) and private_root is None:
        raise ValueError("H requires --private-root")
    cache = Path(cache).resolve()
    if not allow_root and not cache.is_relative_to(CACHE_ROOT):
        raise ValueError("cache root outside allowed root")
    if any((p / ".git").exists() for p in (cache, *cache.parents)):
        raise ValueError("cache root is inside a git work tree")
    cache.mkdir(parents=True, exist_ok=True)
    private = Path(private_root).resolve() if private_root else cache / "build-private"
    if private == cache or cache.is_relative_to(private):
        raise ValueError("private root must be separate from the public cache root")
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(private, 0o700)
    state_path, private_path = cache / "build-state.json", private / "build-state.json"
    catalog_bytes = Path(catalog_path).read_bytes()
    catalog = json.loads(catalog_bytes)
    signature = dict(catalog_sha256=sha(catalog_bytes), want=want, budget_seconds=budget_seconds,
                     candidate_timeout=candidate_timeout)
    if resume:
        state = read_json(private_path)
        if state["signature"] != signature:
            raise ValueError("resume catalog/options do not match")
    else:
        if state_path.exists() or private_path.exists():
            raise ValueError("build state exists; use --resume")
        state = dict(signature=signature, outcomes={}, next_id=1, complete=False, elapsed_seconds=0)
    launch = launch or _launch
    started = time.monotonic()
    previous_elapsed = state["elapsed_seconds"]
    smoke = "SMOKE" in want
    candidates = catalog["cards"]
    records = state["outcomes"]
    public_assignment = state.get("public_assignment", {"pools": {}})

    def save():
        state["elapsed_seconds"] = previous_elapsed + time.monotonic() - started
        _atomic_json(private_path, state)
        os.chmod(private_path, 0o600)
        public = dict(schema_version="eidos-card-build/0.1", signature=signature,
                      outcomes={i: {k: r[k] for k in ("status", "reason") if k in r} for i, r in records.items()},
                      complete=state["complete"], elapsed_seconds=state["elapsed_seconds"],
                      pools=public_assignment.get("pools", {}),
                      held_out_or_reserve=sum(r["status"] == "qualified" for r in records.values()) -
                      sum(len(cards) for cards in public_assignment.get("pools", {}).values()))
        _atomic_json(state_path, public)

    def accepted():
        return [r for r in records.values() if "card_id" in r]

    def qualified():
        return [r for r in records.values() if r["status"] == "qualified"]

    def enough():
        ready = qualified()
        if smoke:
            return {r["candidate"]["task_type"] for r in ready} == {"classification", "regression"}
        cap = sum(pools.null_limit(p, n) for p, n in want.items())
        strong = sum(not r["null_card"] for r in ready)
        nulls = len(ready) - strong
        return strong + min(nulls, cap + 6) >= sum(want.values()) + 6

    if state["complete"]:
        for pool in public_assignment.get("pools", {}):
            pools.verify(cache / "pools" / pool)
        if want.get("H", 0) and (private / "H/pool.json").exists():
            pools.verify(private / "H")
        return read_json(state_path)

    queue = [i for i in range(len(candidates)) if records.get(str(i), {}).get("status") not in FINISHED]
    active = {}
    next_index = 0
    try:
        while (next_index < len(queue) or active) and not enough():
            while len(active) < workers and next_index < len(queue):
                index = queue[next_index]
                next_index += 1
                candidate = candidates[index]
                kind = candidate["task_type"]
                # OpenML is exclusively a regression supplement, after all PMLB regression candidates.
                counts = Counter(r["candidate"]["task_type"] for r in qualified())
                if candidate["source"] == "openml":
                    if any(candidates[i]["source"] == "pmlb" for i in active):
                        next_index -= 1
                        break
                    in_flight = sum(candidates[i]["source"] == "openml" for i in active)
                    if counts["regression"] + in_flight >= counts["classification"] and in_flight:
                        next_index -= 1
                        break
                    if counts["regression"] >= counts["classification"]:
                        records[str(index)] = dict(status="skipped_balance", candidate=candidate)
                        save()
                        continue
                if smoke and counts[kind]:
                    records[str(index)] = dict(status="surplus_task", candidate=candidate)
                    save()
                    continue
                if smoke and any(candidates[i]["task_type"] == kind for i in active):
                    # One in flight per task type prevents speculative extra SMOKE cards.
                    next_index -= 1
                    break
                old = records.get(str(index), {})
                request = dict(candidate=candidate, cache=str(cache), allow_root=allow_root, seed=catalog["seed"],
                               root=str(private / "staging"), budget_seconds=budget_seconds)
                action = "build" if "card_id" in old else "preflight"
                if action == "build":
                    request.update(card_id=old["card_id"], source_dir=old["source_dir"])
                process, response = launch(action, request, private / "jobs" / str(index))
                active[index] = dict(action=action, process=process, response=response, request=request,
                                     started=time.monotonic())
                records[str(index)] = dict(old, status="running", candidate=candidate)
                save()
            # Preflight commits follow catalog order, independent of completion speed.
            pending_preflight = [i for i, job in active.items() if job["action"] == "preflight"]
            first_preflight = min(pending_preflight, default=None)
            progress = False
            for index, job in list(active.items()):
                expired = time.monotonic() - job["started"] >= candidate_timeout
                if expired:
                    _stop(job["process"])
                    result = dict(ok=False, reason="candidate hard wall limit")
                elif job["process"].poll() is None or (job["action"] == "preflight" and index != first_preflight):
                    continue
                else:
                    result = read_json(job["response"]) if job["response"].exists() else dict(ok=False, reason="worker failed")
                progress = True
                record = records[str(index)]
                if not result["ok"]:
                    record.update(status="rejected", reason=result["reason"])
                    del active[index]
                elif job["action"] == "preflight":
                    reason = None
                    if result["fingerprint"] in {r["fingerprint"] for r in accepted()}:
                        reason = "duplicate table content"
                    elif result["imbalanced"] and sum(r.get("imbalanced", False) for r in accepted()) >= 4:
                        reason = "minority class fraction/imbalanced cap"
                    if reason:
                        record.update(status="rejected", reason=reason)
                        del active[index]
                    else:
                        record.update(result, card_id=f"R{state['next_id']:03d}")
                        state["next_id"] += 1
                        job["request"].update(card_id=record["card_id"], source_dir=result["source_dir"])
                        process, response = launch("build", job["request"], private / "jobs" / str(index))
                        job.update(action="build", process=process, response=response)
                else:
                    record.update(result, status="qualified" if result["qualified"] else "unqualified")
                    if not result["qualified"]:
                        failed = result.get("failed_checks", [])
                        record["reason"] = "qualification checks failed" + (": " + ", ".join(failed) if failed else "")
                    del active[index]
                save()
            if not progress:
                time.sleep(0.05)
    finally:
        for index, job in active.items():
            _stop(job["process"])
            records[str(index)]["status"] = "interrupted"
        save()

    ready = []
    for r in qualified():
        ready.append(dict(r["candidate"], card_id=r["card_id"], null_card=r["null_card"],
                          imbalanced=r["imbalanced"], n_features=r["n_features"]))
    # Do not let completion order influence seeded pool assignment.
    ready.sort(key=lambda c: c["card_id"])
    sizes = dict(want)
    if smoke:
        selected = []
        for kind in ("classification", "regression"):
            selected.extend([c for c in ready if c["task_type"] == kind][:1])
        # SMOKE is a public diagnostic pool, and uses the same family/null-aware assignment machinery.
        sizes = dict(SMOKE=len(selected), H=0)
        ready_for_assignment = selected
    else:
        sizes.setdefault("H", 0)
        ready_for_assignment = ready
    public_assignment = pools.assign(ready_for_assignment, sizes, catalog["seed"], private / "assignment.seed",
                                     allow_shortfall=True)
    private_assignment = read_json(private / "assignments.json")
    for pool, members in private_assignment["pools"].items():
        if pool == "H" and not want.get("H", 0):
            continue
        destination = private / "H" if pool == "H" else cache / "pools" / pool
        if (destination / "pool.json").exists():
            existing = read_json(destination / "pool.json")
            if existing["card_ids"] != [c["card_id"] for c in members]:
                raise ValueError("existing pool does not match resumed assignment")
        else:
            if destination.exists():
                shutil.rmtree(destination)  # an interrupted assembly, under this build's output root
            pools.build_pool(pool, [private / "staging/seal" / c["card_id"] for c in members], destination)
        pools.verify(destination)
    actual = {pool: len(private_assignment["pools"].get(pool, [])) for pool in want}
    state.update(complete=True, public_assignment=public_assignment,
                 shortfall={pool: want[pool] - actual[pool] for pool in want}, balance=dict(Counter(c["task_type"] for c in ready)))
    save()
    public = read_json(state_path)
    public.update(shortfall=state["shortfall"], balance=state["balance"])
    _atomic_json(state_path, public)
    return public


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog")
    parser.add_argument("--cache")
    parser.add_argument("--want", type=parse_want, default=parse_want("D=8,E1=8,E2=8,E3=8,H=12"))
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--budget-seconds", type=float, default=480)
    parser.add_argument("--private-root")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--candidate-timeout", type=float, default=1500)
    parser.add_argument("--allow-root", action="store_true")
    parser.add_argument("--worker-action", choices=["preflight", "build"], help=argparse.SUPPRESS)
    parser.add_argument("--request", help=argparse.SUPPRESS)
    parser.add_argument("--response", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker_action:
        _worker(args.worker_action, args.request, args.response)
        return
    if not args.catalog or not args.cache:
        parser.error("--catalog and --cache are required")
    try:
        result = build(args.catalog, args.cache, args.want, workers=args.workers, budget_seconds=args.budget_seconds,
                       private_root=args.private_root, resume=args.resume, candidate_timeout=args.candidate_timeout,
                       allow_root=args.allow_root)
    except Exception as error:
        # Tracebacks from private verification can contain H paths. Keep them private too.
        private = Path(args.private_root) if args.private_root else Path(args.cache) / "build-private"
        if private.is_dir():
            (private / "driver-error.log").write_text(traceback.format_exc())
        message = f"build failed: {_safe_reason(error)}; details remain under the private root"
        print(message)
        raise SystemExit(1) from None
    message = json.dumps(result, sort_keys=True)
    (Path(args.cache) / "build.log").write_text(message + "\n")
    print(message)


if __name__ == "__main__":
    main()
