"""Author tests for eidos.search.recipe-sweep (not independent qualification).

Every test runs the real worker protocol against a tiny synthetic project: a few lines of Python written to a source
bundle, a deterministic function of its parameters, seed and fold, that writes its metrics to {out}/receipt.json. The
expected values are re-derived here from the project's formula, never read back from the component.
"""

from __future__ import annotations

import hashlib
import importlib.util
import itertools
import json
import math
import os
import signal
import statistics
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import COMPONENTS, ROOT, Result, run_component
from eidos_wire import OutputManifest
from eidos_worker.testing import make_invocation

NAME = "eidos.search.recipe-sweep"
MANIFEST = json.loads((COMPONENTS / NAME / "eidos-component.json").read_text())
ENTRY = MANIFEST["entry"]

# -- the synthetic projects ----------------------------------------------------------------------------------------
FORMULA = textwrap.dedent(
    """
    import json, sys
    a = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    lr, depth = float(a["--lr"]), int(a["--depth"])
    seed, fold, folds = int(a["--seed"]), int(a["--fold"]), int(a["--folds"])
    params = json.loads(a["--params"])
    score = 10 - 50 * (lr - 0.2) ** 2 - depth + 0.1 * seed + 0.01 * fold
    json.dump({"validation_score": score, "validation_loss": 10 - score, "seen_seed": seed, "seen_fold": fold,
               "seen_folds": folds, "params_match": float(params == {"lr": lr, "depth": depth})},
              open(a["--out"] + "/receipt.json", "w"))
    """
)
FORMULA_ARGV = ["--out", "{out}", "--lr", "{param.lr}", "--depth", "{param.depth}", "--seed", "{seed}",
                "--fold", "{fold}", "--folds", "{folds}", "--params", "{params_json}"]

# One value per step: a number is the score; a word makes the command misbehave in one specific way.
TABLE = textwrap.dedent(
    """
    import json, sys
    a = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    VALUES = __VALUES__
    v = VALUES[int(a["--step"])]
    out = a["--out"] + "/receipt.json"
    if v == "fail":
        sys.stderr.write("boom at step " + a["--step"])
        sys.exit(3)
    if v == "omit":
        json.dump({"other_metric": 1.0}, open(out, "w"))
    elif v == "nan":
        json.dump({"validation_score": float("nan")}, open(out, "w"))
    elif v == "inf":
        json.dump({"validation_score": float("inf")}, open(out, "w"))
    elif v == "sidenan":
        json.dump({"validation_score": 7.0, "grad_norm": float("nan")}, open(out, "w"))
    elif v == "norcpt":
        pass
    else:
        json.dump({"validation_score": v}, open(out, "w"))
    """
)

# Equal means with different spreads: the seed's parity sets the sign of the spread.
TIE = textwrap.dedent(
    """
    import json, sys
    a = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    base, spread = {0: (5, 2), 1: (5, 1), 2: (5, 1), 3: (4, 0), 4: (6, 0)}[int(a["--case"])]
    sign = 1 if int(a["--seed"]) % 2 else -1
    json.dump({"validation_score": base + sign * spread}, open(a["--out"] + "/receipt.json", "w"))
    """
)

RANDOM = textwrap.dedent(
    """
    import json, math, sys
    a = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    lr, layers, act = float(a["--lr"]), int(a["--layers"]), a["--act"]
    score = -abs(math.log10(lr) + 2) - 0.1 * layers + (0.05 if act == "relu" else 0.0)
    json.dump({"validation_score": score}, open(a["--out"] + "/receipt.json", "w"))
    """
)

SMALL = textwrap.dedent(
    """
    import json, sys
    a = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    json.dump({"validation_score": int(a["--a"]) * 10 + (1 if a["--b"] == "x" else 0)},
              open(a["--out"] + "/receipt.json", "w"))
    """
)

# One recipe is fast, one has a slow second seed, and a third is never reached.
NAP = textwrap.dedent(
    """
    import json, sys, time
    a = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    mode, seed = a["--mode"], int(a["--seed"])
    if mode == "slow" and seed == 2:
        time.sleep(60)
    json.dump({"validation_score": 100.0 if mode == "slow" else 1.0}, open(a["--out"] + "/receipt.json", "w"))
    """
)

NAP_EACH = textwrap.dedent(
    """
    import json, sys, time
    a = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    time.sleep(0.25)
    json.dump({"validation_score": float(a["--idx"])}, open(a["--out"] + "/receipt.json", "w"))
    """
)

SEED_FAIL = textwrap.dedent(
    """
    import json, sys
    a = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    if int(a["--case"]) == 1 and int(a["--seed"]) == 2:
        sys.stderr.write("seed 2 diverged")
        sys.exit(2)
    json.dump({"validation_score": float(a["--case"]) + int(a["--seed"]) / 100}, open(a["--out"] + "/receipt.json", "w"))
    """
)

LEAKY = textwrap.dedent(
    """
    import json, sys
    a = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    m = {"validation_score": 1.0, a["--leak"]: 0.8765431}
    json.dump(m, open(a["--out"] + "/receipt.json", "w"))
    """
)

RECIPE = textwrap.dedent(
    """
    import json, sys
    a = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    recipe = json.load(open(a["--recipe"]))
    json.dump(recipe, open(a["--out"] + "/recipe_seen.json", "w"))
    json.dump({"validation_score": recipe["optimizer"]["lr"] * 10, "n_layers": len(recipe["layers"])},
              open(a["--out"] + "/receipt.json", "w"))
    """
)

STAGING = textwrap.dedent(
    """
    import json, os, sys
    a = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    m = {"has_" + n.replace(".", "_"): float(os.path.exists(n))
         for n in ("train.npz", "validation.npz", "task.json", "tr.bin", "notes.txt", "data.npz", "project.py")}
    m["saw_leak"] = float(os.path.exists("leak.txt"))
    open("leak.txt", "w").write("x")
    m["validation_score"] = 1.0
    json.dump(m, open(a["--out"] + "/receipt.json", "w"))
    """
)

FORMATS = textwrap.dedent(
    """
    import json, sys
    a = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    cfg = json.loads(a["--cfg"])
    ok = (a["--flag"] == "true" and a["--name"] == "adam" and json.loads(a["--dims"]) == [1, 2]
          and a["--lr"] == "0.001" and cfg == {"k": 1})
    json.dump({"validation_score": float(ok), "k": cfg["k"]}, open(a["--out"] + "/receipt.json", "w"))
    """
)


def table_project(values):
    return TABLE.replace("__VALUES__", repr(list(values)))


# -- config and result helpers ---------------------------------------------------------------------------------------
def make_config(argv, parameters, *, objective="validation_score", direction=None, seeds=None, folds=None,
                budget=None, stopping=None, space=None, command=None, **extra):
    objective_block = {"metric": objective}
    if direction:
        objective_block["direction"] = direction
    config = {"command": {"argv": ["python", "project.py", *argv], **(command or {})},
              "space": {"mode": "grid", "parameters": parameters, **(space or {})},
              "objective": objective_block, "budget": {"max_trials": 100} if budget is None else budget}
    policy = {}
    if seeds is not None:
        policy["seeds"] = seeds
    if folds is not None:
        policy["folds"] = folds
    if policy:
        config["seed_policy"] = policy
    if stopping is not None:
        config["stopping"] = stopping
    config.update(extra)
    return config


def formula_config(**kw):
    # lr is listed before depth on purpose: the component evaluates parameters in sorted name order.
    kw.setdefault("seeds", [1, 2])
    kw.setdefault("folds", 2)
    parameters = {"lr": {"values": [0.1, 0.4, 0.2]}, "depth": {"values": [2, 1]}}
    return make_config(FORMULA_ARGV, parameters, **kw)


def table_config(values, **kw):
    argv = ["--out", "{out}", "--step", "{param.step}"]
    return make_config(argv, {"step": {"values": list(range(len(values)))}}, **kw)


def tie_config(**kw):
    kw.setdefault("seeds", [1, 2])
    return make_config(["--out", "{out}", "--case", "{param.case}", "--seed", "{seed}"],
                       {"case": {"values": [0, 1, 2, 3, 4]}}, **kw)


def random_config(seed=7, **kw):
    argv = ["--out", "{out}", "--lr", "{param.lr}", "--layers", "{param.layers}", "--act", "{param.act}"]
    parameters = {"lr": {"low": 1e-4, "high": 1e-1, "log": True},
                  "layers": {"low": 1, "high": 6, "integer": True},
                  "act": {"values": ["relu", "tanh"]}}
    space = {"mode": "random", **({} if seed is None else {"seed": seed}), **kw.pop("space", {})}
    return make_config(argv, parameters, space=space, **kw)


def in_order(table):
    return sorted(table["recipes"], key=lambda r: r["index"])


def steps(table):
    return [r["parameters"]["step"] for r in in_order(table)]


def refused(res, *fragments):
    assert res.manifest.status == "failed", res.receipt
    message = res.manifest.error["message"]
    for fragment in fragments:
        assert fragment in message, message
    return message


def run_with(tmp_path, label, inputs, config, *, limits=None, seed=0, cwd=None):
    """run_component with a job limits block and a job seed, which run_component does not expose."""
    root = tmp_path / label
    inv = make_invocation(root, ENTRY, inputs=inputs, config=config, source_dir=COMPONENTS / NAME, component_id=NAME,
                          limits=limits, seed=seed)
    path = root / "invocation.json"
    inv.write(path)
    proc = subprocess.run([sys.executable, "-m", "eidos_worker.run", str(path)], capture_output=True, text=True,
                          cwd=cwd, timeout=240)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return Result(OutputManifest.read(inv.staging_dir), inv, proc.stderr)


@pytest.fixture()
def sweep(tmp_path):
    """sweep(project, config, ...) runs the component once; each call gets its own run directory."""
    counter = {"n": 0}

    def go(project, config, *, files=None, inputs=None, cwd=None, limits=None, seed=None):
        counter["n"] += 1
        label = f"s{counter['n']}"
        bound = dict(inputs or {})
        if files is not None or project is not None:
            bound["source"] = {"files": files if files is not None else {"project.py": project}}
        if limits is None and seed is None:
            return run_component(tmp_path, NAME, ENTRY, inputs=bound, config=config, cwd=cwd, label=label)
        return run_with(tmp_path, label, bound, config, limits=limits, seed=0 if seed is None else seed, cwd=cwd)

    return go


# -- the grid's expected values, derived from the project's formula -----------------------------------------------------
SEEDS, FOLDS = [1, 2], 2
DEPTHS, LRS = [2, 1], [0.1, 0.4, 0.2]
GRID_ORDER = [{"depth": d, "lr": lr} for d in DEPTHS for lr in LRS]  # parameter names sorted: depth, then lr


def formula(lr, depth, seed, fold):
    return 10 - 50 * (lr - 0.2) ** 2 - depth + 0.1 * seed + 0.01 * fold


def expected_recipe(parameters, seeds=SEEDS, folds=FOLDS):
    pairs = list(itertools.product(seeds, range(folds)))
    values = [formula(parameters["lr"], parameters["depth"], s, f) for s, f in pairs]
    return {"pairs": pairs, "values": values, "mean": statistics.fmean(values),
            "std": statistics.stdev(values) if len(values) > 1 else 0.0}


def approx(x):
    return pytest.approx(x, abs=1e-9)


def assert_grid_documents(table, best):
    """The staged documents of the formula grid, checked against what the formula gives."""
    recipes = in_order(table)
    assert [r["parameters"] for r in recipes] == GRID_ORDER
    assert [r["index"] for r in recipes] == list(range(6))
    assert len({r["recipe_id"] for r in recipes}) == 6
    expected = [expected_recipe(p) for p in GRID_ORDER]
    for row, want in zip(recipes, expected, strict=True):
        assert row["status"] == "ok"
        assert row["recipe_id"].startswith(f"r{row['index']:04d}-")
        assert [(e["seed"], e["fold"]) for e in row["evaluations"]] == want["pairs"]
        assert [e["status"] for e in row["evaluations"]] == ["ok"] * 4
        assert [e["metrics"]["validation_score"] for e in row["evaluations"]] == [approx(v) for v in want["values"]]
        score = row["metrics"]["validation_score"]
        assert score["mean"] == approx(want["mean"])
        assert score["std"] == approx(want["std"])
        assert score["n"] == 4
        assert row["metrics"]["validation_loss"]["mean"] == approx(10 - want["mean"])
        assert row["objective"] == {**score, "metric": "validation_score"}
    ranking = sorted(range(6), key=lambda i: -expected[i]["mean"])
    assert [r["index"] for r in table["recipes"]] == ranking  # the table lists recipes best first
    assert [r["rank"] for r in table["recipes"]] == [1, 2, 3, 4, 5, 6]
    winner = {"depth": 1, "lr": 0.2}  # the true optimum of the formula
    assert table["recipes"][0]["parameters"] == winner == max(GRID_ORDER, key=lambda p: expected_recipe(p)["mean"])
    assert table["recipes"][0]["objective"]["mean"] == approx(9 + 0.1 * 1.5 + 0.01 * 0.5)
    assert best["status"] == "found"
    assert best["recipe"]["parameters"] == winner
    assert best["recipe"]["recipe_id"] == table["recipes"][0]["recipe_id"]


@pytest.fixture(scope="module")
def grid(tmp_path_factory):
    inputs = {"source": {"files": {"project.py": FORMULA}}}
    config = formula_config()
    res = run_component(tmp_path_factory.mktemp("grid"), NAME, ENTRY, inputs=inputs, config=config)
    assert res.ok, res.manifest.error
    return res, config


# -- grid -----------------------------------------------------------------------------------------------------------------
def test_manifest_declares_the_two_outputs_and_the_stageable_slots(grid):
    res, _ = grid
    assert {"evidence_table", "best_recipe"} <= set(res.slots())
    assert res.json("evidence_table")["schema"] == "recipe-sweep-table/1"
    assert res.json("best_recipe")["schema"] == "recipe-sweep-best/1"


def test_grid_evaluates_every_recipe_in_sorted_parameter_cartesian_order(grid):
    res, config = grid
    table = res.json("evidence_table")
    recipes = in_order(table)
    assert [r["parameters"] for r in recipes] == GRID_ORDER  # depth (slowest) then lr, values in listed order
    assert table["counts"] == {"space_size": 6, "evaluated": 6, "usable": 6, "failed": 0}
    assert table["stop_reason"] == "space-exhausted"
    assert table["space"] == config["space"]
    assert table["seed_policy"] == {"seeds": SEEDS, "folds": FOLDS}
    assert table["budget"] == {"max_trials": 100}


def test_grid_ranking_and_statistics_match_the_formula(grid):
    res, _ = grid
    assert_grid_documents(res.json("evidence_table"), res.json("best_recipe"))


def test_best_recipe_names_the_optimum_and_carries_what_is_needed_to_lock_it(grid):
    res, config = grid
    best, table = res.json("best_recipe"), res.json("evidence_table")
    assert best["status"] == "found"
    assert best["recipe"]["parameters"] == {"depth": 1, "lr": 0.2}
    assert best["recipe"]["recipe_id"].startswith("r0005-")  # the sixth recipe in evaluation order
    assert best["recipe"]["objective"]["mean"] == approx(9.155)
    assert len(best["recipe"]["evaluations"]) == 4
    assert best["recipe"]["command"] == config["command"]
    assert best["recipe"]["recipe_template"] is None
    assert best["lock_required"] is True
    assert best["objective"] == {"metric": "validation_score", "direction": "maximize"}
    assert res.receipt["best_recipe_id"] == best["recipe"]["recipe_id"]
    assert res.receipt["metrics"] == {"recipes_evaluated": 6, "recipes_usable": 6, "recipes_failed": 0,
                                      "best_objective": approx(9.155)}
    assert res.receipt["stop_reason"] == "space-exhausted"
    assert table["objective"] == best["objective"]


def test_documents_are_exploratory_and_make_no_claim(grid):
    res, _ = grid
    for slot in ("evidence_table", "best_recipe"):
        doc = res.json(slot)
        assert doc["exploratory"] is True
        assert doc["claim"] is None
        assert doc["selection_split"] == "visible-validation"
        assert "exploratory" in doc["disclaimer"].lower() and "no claim" in doc["disclaimer"].lower()
    assert res.receipt["exploratory"] is True


def test_command_sees_seed_fold_folds_and_params_json(grid):
    res, _ = grid
    for row in res.json("evidence_table")["recipes"]:
        for e in row["evaluations"]:
            assert e["metrics"]["seen_seed"] == e["seed"]
            assert e["metrics"]["seen_fold"] == e["fold"]
            assert e["metrics"]["seen_folds"] == FOLDS
            assert e["metrics"]["params_match"] == 1.0  # {params_json} was the recipe's parameters


# -- minimize, ties ---------------------------------------------------------------------------------------------------
def test_minimize_ranks_the_lowest_objective_first(sweep):
    config = formula_config(objective="validation_loss", direction="minimize", seeds=[1], folds=1)
    res = sweep(FORMULA, config)
    assert res.ok, res.manifest.error
    table, best = res.json("evidence_table"), res.json("best_recipe")
    loss = {tuple(sorted(p.items())): 10 - formula(p["lr"], p["depth"], 1, 0) for p in GRID_ORDER}
    ranked = [tuple(sorted(r["parameters"].items())) for r in table["recipes"]]
    assert ranked == sorted(loss, key=loss.get)  # smallest loss first
    assert table["recipes"][0]["parameters"] == {"depth": 1, "lr": 0.2}
    assert best["recipe"]["parameters"] == {"depth": 1, "lr": 0.2}
    assert best["recipe"]["objective"]["mean"] == approx(10 - formula(0.2, 1, 1, 0))
    assert table["objective"]["direction"] == best["objective"]["direction"] == "minimize"
    assert res.receipt["metrics"]["best_objective"] == approx(0.9)


def test_direction_decides_the_winner(sweep):
    res = sweep(FORMULA, formula_config(objective="validation_loss", direction="maximize", seeds=[1], folds=1))
    assert res.json("best_recipe")["recipe"]["parameters"] == {"depth": 2, "lr": 0.4}  # the largest loss


def test_maximize_ties_break_to_lower_std_then_earlier_recipe(sweep):
    res = sweep(TIE, tie_config(direction="maximize"))
    table = res.json("evidence_table")
    cases = [r["parameters"]["case"] for r in table["recipes"]]
    # mean 6 first; then the three with mean 5 by std (cases 1 and 2 have 1.41, case 0 has 2.83) and, equal std,
    # by index; mean 4 last
    assert cases == [4, 1, 2, 0, 3]
    by_case = {r["parameters"]["case"]: r for r in table["recipes"]}
    assert [by_case[c]["objective"]["mean"] for c in (0, 1, 2)] == [5.0, 5.0, 5.0]
    assert by_case[1]["objective"]["std"] == by_case[2]["objective"]["std"] == approx(math.sqrt(2))
    assert by_case[0]["objective"]["std"] == approx(math.sqrt(8))  # sample standard deviation of 7 and 3
    assert [r["rank"] for r in table["recipes"]] == [1, 2, 3, 4, 5]
    assert res.json("best_recipe")["recipe"]["parameters"] == {"case": 4}


def test_minimize_ties_break_the_same_way(sweep):
    res = sweep(TIE, tie_config(direction="minimize"))
    table = res.json("evidence_table")
    assert [r["parameters"]["case"] for r in table["recipes"]] == [3, 1, 2, 0, 4]
    assert res.json("best_recipe")["recipe"]["parameters"] == {"case": 3}


# -- seeds x folds ------------------------------------------------------------------------------------------------------
def test_each_recipe_runs_seeds_times_folds_and_the_command_sees_each(sweep):
    seeds, folds = [3, 5, 9], 3
    res = sweep(FORMULA, formula_config(seeds=seeds, folds=folds, budget={"max_trials": 2}))
    assert res.ok, res.manifest.error
    table = res.json("evidence_table")
    assert table["seed_policy"] == {"seeds": seeds, "folds": folds}
    pairs = list(itertools.product(seeds, range(folds)))
    for row in table["recipes"]:
        assert len(row["evaluations"]) == len(seeds) * folds == 9
        assert [(e["seed"], e["fold"]) for e in row["evaluations"]] == pairs
        assert [e["metrics"]["seen_seed"] for e in row["evaluations"]] == [s for s, _ in pairs]
        assert [e["metrics"]["seen_fold"] for e in row["evaluations"]] == [f for _, f in pairs]
        assert {e["metrics"]["seen_folds"] for e in row["evaluations"]} == {3}
        want = expected_recipe(row["parameters"], seeds, folds)
        assert row["objective"]["mean"] == approx(want["mean"])
        assert row["objective"]["std"] == approx(want["std"])
        assert row["objective"]["n"] == 9
        assert row["metrics"]["seen_seed"]["mean"] == approx(statistics.fmean(s for s, _ in pairs))


def test_default_policy_is_one_fold_and_the_job_seed(sweep):
    res = sweep(FORMULA, formula_config(seeds=None, folds=None, budget={"max_trials": 1}), seed=42)
    table = res.json("evidence_table")
    assert table["seed_policy"] == {"seeds": [42], "folds": 1}
    (row,) = table["recipes"]
    assert [(e["seed"], e["fold"]) for e in row["evaluations"]] == [(42, 0)]
    assert row["evaluations"][0]["metrics"]["seen_seed"] == 42
    assert row["evaluations"][0]["metrics"]["seen_folds"] == 1


# -- random -----------------------------------------------------------------------------------------------------------
def random_run(sweep, **kw):
    res = sweep(RANDOM, random_config(**kw))
    assert res.ok, res.manifest.error
    return res, res.json("evidence_table")


def drawn(table):
    return [r["parameters"] for r in in_order(table)]


def test_random_same_seed_reproduces_recipes_and_digest(sweep):
    kw = {"space": {"samples": 12}}
    first, t1 = random_run(sweep, seed=7, **kw)
    second, t2 = random_run(sweep, seed=7, **kw)
    assert len(drawn(t1)) == 12 and drawn(t1) == drawn(t2)
    assert first.receipt["evidence_digest"] == second.receipt["evidence_digest"]
    assert [r["recipe_id"] for r in in_order(t1)] == [r["recipe_id"] for r in in_order(t2)]


def test_random_different_seed_draws_different_recipes(sweep):
    first, t7 = random_run(sweep, seed=7, space={"samples": 12})
    other, t8 = random_run(sweep, seed=8, space={"samples": 12})
    assert drawn(t7) != drawn(t8)
    assert first.receipt["evidence_digest"] != other.receipt["evidence_digest"]


def test_random_ranges_stay_in_bounds_and_the_log_scale_is_logarithmic(sweep):
    res, table = random_run(sweep, seed=11, space={"samples": 40})
    recipes = drawn(table)
    assert len(recipes) == 40
    for p in recipes:
        assert 1e-4 <= p["lr"] <= 1e-1
        assert type(p["layers"]) is int and 1 <= p["layers"] <= 6
        assert p["act"] in ("relu", "tanh")
    assert len({p["layers"] for p in recipes}) >= 4
    # on a log scale a third of the draws fall in the lowest decade of three; on a linear scale under 1 percent would
    assert sum(p["lr"] < 1e-3 for p in recipes) >= 5
    # every ranked value follows from the project's formula and the recorded parameters
    for row in table["recipes"]:
        p = row["parameters"]
        want = -abs(math.log10(p["lr"]) + 2) - 0.1 * p["layers"] + (0.05 if p["act"] == "relu" else 0.0)
        assert row["objective"]["mean"] == approx(want)
    best = max(recipes, key=lambda p: -abs(math.log10(p["lr"]) + 2) - 0.1 * p["layers"] + (0.05 if p["act"] == "relu" else 0))
    assert res.json("best_recipe")["recipe"]["parameters"] == best


def test_random_samples_default_to_max_trials_and_end_with_the_budget(sweep):
    res, table = random_run(sweep, seed=5, budget={"max_trials": 8})
    assert len(table["recipes"]) == 8
    assert table["stop_reason"] == "budget-trials"
    assert table["counts"]["space_size"] is None  # a random space has no known size
    res, table = random_run(sweep, seed=5, budget={"max_trials": 100}, space={"samples": 5})
    assert len(table["recipes"]) == 5
    assert table["stop_reason"] == "space-exhausted"


def test_random_skips_repeated_draws(sweep):
    parameters = {"a": {"values": [1, 2]}, "b": {"values": ["x", "y"]}}
    argv = ["--out", "{out}", "--a", "{param.a}", "--b", "{param.b}"]
    config = make_config(argv, parameters, mode="random", space={"mode": "random", "seed": 3, "samples": 10},
                         budget={"max_trials": 10})
    res = sweep(SMALL, config)
    assert res.ok, res.manifest.error
    table = res.json("evidence_table")
    recipes = drawn(table)
    assert len(recipes) == 4  # four distinct recipes exist; ten were asked for and none was measured twice
    assert sorted(json.dumps(p, sort_keys=True) for p in recipes) == sorted(
        json.dumps({"a": a, "b": b}, sort_keys=True) for a in (1, 2) for b in ("x", "y"))
    assert table["stop_reason"] == "space-exhausted"
    assert res.json("best_recipe")["recipe"]["parameters"] == {"a": 2, "b": "x"}
    # no samples and only a seconds budget: the finite space still ends the search
    config = make_config(argv, parameters, space={"mode": "random", "seed": 3}, budget={"max_seconds": 60})
    res = sweep(SMALL, config)
    assert len(drawn(res.json("evidence_table"))) == 4 and res.receipt["stop_reason"] == "space-exhausted"


def test_random_without_a_space_seed_is_refused(sweep):
    res = sweep(RANDOM, random_config(seed=None, space={"samples": 3}))
    refused(res, "space.seed")
    assert "evidence_table" not in res.slots()


def test_random_accepts_seed_zero(sweep):
    res, table = random_run(sweep, seed=0, space={"samples": 3})
    assert len(table["recipes"]) == 3


# -- determinism -----------------------------------------------------------------------------------------------------
def recomputed_digest(table):
    stable = [{**r, "evaluations": [{k: v for k, v in e.items() if k != "seconds"} for e in r["evaluations"]]}
              for r in table["recipes"]]
    return "sha256:" + hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def test_two_full_runs_give_the_same_evidence_digest(grid, sweep):
    first, config = grid
    second = sweep(FORMULA, config)
    assert second.ok, second.manifest.error
    digest = first.receipt["evidence_digest"]
    assert digest == second.receipt["evidence_digest"]
    assert digest.startswith("sha256:") and len(digest) == len("sha256:") + 64
    assert first.receipt["best_recipe_id"] == second.receipt["best_recipe_id"]


def test_digest_covers_everything_recorded_except_wall_seconds(grid, sweep):
    first, config = grid
    table = first.json("evidence_table")
    assert first.receipt["evidence_digest"] == recomputed_digest(table)
    # a project that gives different results gives a different digest
    changed = sweep(FORMULA.replace("- depth +", "- 2 * depth +"), config)
    assert changed.receipt["evidence_digest"] != first.receipt["evidence_digest"]


# -- budget ------------------------------------------------------------------------------------------------------------
def test_max_trials_stops_at_that_many_recipes(sweep):
    res = sweep(FORMULA, formula_config(seeds=[1], folds=1, budget={"max_trials": 3}))
    table = res.json("evidence_table")
    assert table["stop_reason"] == res.receipt["stop_reason"] == "budget-trials"
    assert [r["parameters"] for r in in_order(table)] == GRID_ORDER[:3]
    assert table["counts"] == {"space_size": 6, "evaluated": 3, "usable": 3, "failed": 0}
    assert table["budget"] == {"max_trials": 3}
    assert res.receipt["metrics"]["recipes_evaluated"] == 3
    want = max(GRID_ORDER[:3], key=lambda p: formula(p["lr"], p["depth"], 1, 0))
    assert res.json("best_recipe")["recipe"]["parameters"] == want  # the best of those it had time to try


@pytest.mark.parametrize("max_trials, reason", [(6, "space-exhausted"), (50, "space-exhausted"), (5, "budget-trials"),
                                                (1, "budget-trials")])
def test_a_budget_that_covers_the_whole_grid_is_not_a_budget_stop(sweep, max_trials, reason):
    res = sweep(FORMULA, formula_config(seeds=[1], folds=1, budget={"max_trials": max_trials}))
    assert res.receipt["stop_reason"] == reason
    assert res.json("evidence_table")["counts"]["evaluated"] == min(max_trials, 6)


def test_max_seconds_stops_the_search_and_records_the_partial_recipe_as_failed(sweep):
    parameters = {"mode": {"values": ["fast", "slow", "tail"]}}
    config = make_config(["--out", "{out}", "--mode", "{param.mode}", "--seed", "{seed}"], parameters,
                         seeds=[1, 2, 3], budget={"max_seconds": 1})
    res = sweep(NAP, config)
    assert res.ok, res.manifest.error  # running out of time is an outcome, not a failure
    table = res.json("evidence_table")
    assert table["stop_reason"] == res.receipt["stop_reason"] == "budget-seconds"
    assert table["budget"] == {"max_seconds": 1}
    fast, slow = in_order(table)  # the third recipe was never reached
    assert (fast["parameters"], fast["status"], fast["rank"]) == ({"mode": "fast"}, "ok", 1)
    assert slow["parameters"] == {"mode": "slow"}
    assert slow["status"] == "failed" and slow["rank"] is None and slow["objective"] is None
    assert slow["error"]
    # the slow recipe's first seed scored 100, far above the fast one, and the partial recipe is still unranked
    assert slow["evaluations"][0]["status"] == "ok" and slow["evaluations"][0]["metrics"]["validation_score"] == 100.0
    assert slow["evaluations"][1]["status"] == "timeout"
    assert table["counts"] == {"space_size": 3, "evaluated": 2, "usable": 1, "failed": 1}
    assert [r["parameters"]["mode"] for r in table["recipes"]] == ["fast", "slow"]  # ranked first, failed after
    assert res.json("best_recipe")["recipe"]["parameters"] == {"mode": "fast"}
    assert res.receipt["metrics"] == {"recipes_evaluated": 2, "recipes_usable": 1, "recipes_failed": 1,
                                      "best_objective": 1.0}


@pytest.mark.parametrize("budget", [{}, None], ids=["empty", "absent"])
def test_no_budget_at_all_is_refused(sweep, budget):
    config = table_config([1.0, 2.0], budget=budget)
    if budget is None:
        del config["budget"]
    res = sweep(table_project([1.0, 2.0]), config)
    refused(res, "max_trials or max_seconds")
    assert "evidence_table" not in res.slots()
    # a job wall limit does not stand in for the search's own budget
    res = sweep(table_project([1.0, 2.0]), config, limits={"wall_seconds": 60})
    refused(res, "max_trials or max_seconds")


@pytest.mark.parametrize("budget, wall, recorded", [
    ({"max_trials": 2}, 100, {"max_trials": 2, "max_seconds": 90.0}),  # 90 percent of the job's wall time
    ({"max_seconds": 500}, 100, {"max_seconds": 90.0}),  # the job limit caps a larger request
    ({"max_seconds": 10}, 100, {"max_seconds": 10.0}),  # and never raises a smaller one
    ({"max_trials": 2}, 1, {"max_trials": 2, "max_seconds": 1.0}),  # at least a second
])
def test_job_wall_limit_caps_the_recorded_budget(sweep, budget, wall, recorded):
    res = sweep(table_project([1.0, 2.0, 3.0]), table_config([1.0, 2.0, 3.0], budget=budget),
                limits={"wall_seconds": wall})
    assert res.ok, res.manifest.error
    assert res.json("evidence_table")["budget"] == recorded


def test_job_wall_limit_actually_ends_the_search(sweep):
    config = make_config(["--out", "{out}", "--idx", "{param.idx}"], {"idx": {"values": list(range(12))}},
                         budget={"max_trials": 50})
    res = sweep(NAP_EACH, config, limits={"wall_seconds": 1})
    assert res.ok, res.manifest.error
    table = res.json("evidence_table")
    assert table["stop_reason"] == "budget-seconds"
    assert table["budget"] == {"max_trials": 50, "max_seconds": 1.0}
    assert 1 <= len(table["recipes"]) < 12


# -- stopping ----------------------------------------------------------------------------------------------------------
CLIMB = [1.0, 3.0, 3.2, 3.4, 10.0, 3.0]


def stopped(sweep, values, **kw):
    res = sweep(table_project(values), table_config(values, **kw))
    assert res.ok, res.manifest.error
    table = res.json("evidence_table")
    assert res.receipt["stop_reason"] == table["stop_reason"]
    return res, table


def test_no_stopping_rule_runs_the_whole_space(sweep):
    res, table = stopped(sweep, CLIMB)
    assert steps(table) == list(range(6)) and table["stop_reason"] == "space-exhausted"
    assert table["stopping"] == {"rule": "none"}
    assert res.json("best_recipe")["recipe"]["parameters"] == {"step": 4}


def test_patience_with_min_delta_ends_the_search_at_the_right_recipe(sweep):
    stopping = {"rule": "patience", "patience": 2, "min_delta": 0.5}
    res, table = stopped(sweep, CLIMB, stopping=stopping)
    # 3.0 improves on 1.0; 3.2 and 3.4 do not beat 3.0 by 0.5: two in a row, so the search ends after step 3
    assert steps(table) == [0, 1, 2, 3]
    assert table["stop_reason"] == "stopping-patience"
    assert table["stopping"] == stopping
    assert table["counts"]["space_size"] == 6 and table["counts"]["evaluated"] == 4
    # the winner is the best recipe measured, not the last one that improved the running best
    assert res.json("best_recipe")["recipe"]["parameters"] == {"step": 3}


def test_patience_without_min_delta_keeps_going_while_anything_improves(sweep):
    res, table = stopped(sweep, CLIMB, stopping={"rule": "patience", "patience": 2})
    assert steps(table) == list(range(6))
    assert table["stop_reason"] == "space-exhausted"
    assert res.json("best_recipe")["recipe"]["parameters"] == {"step": 4}


def test_patience_of_one_stops_at_the_first_recipe_that_does_not_improve(sweep):
    res, table = stopped(sweep, [1.0, 3.0, 2.0, 9.0], stopping={"rule": "patience", "patience": 1})
    assert steps(table) == [0, 1, 2]
    assert table["stop_reason"] == "stopping-patience"
    # the rule fires before the space is exhausted, and also when it fires on the very last recipe
    res, table = stopped(sweep, CLIMB, stopping={"rule": "patience", "patience": 1})
    assert steps(table) == list(range(6)) and table["stop_reason"] == "stopping-patience"


def test_patience_follows_the_direction(sweep):
    values = [5.0, 4.0, 4.8, 4.9, 1.0]
    res, table = stopped(sweep, values, direction="minimize", stopping={"rule": "patience", "patience": 2})
    assert steps(table) == [0, 1, 2, 3]
    assert table["stop_reason"] == "stopping-patience"
    assert res.json("best_recipe")["recipe"]["parameters"] == {"step": 1}


def test_failed_recipes_count_against_patience(sweep):
    res, table = stopped(sweep, [3.0, "fail", "fail", 9.0], stopping={"rule": "patience", "patience": 2})
    assert steps(table) == [0, 1, 2] and table["stop_reason"] == "stopping-patience"
    assert [r["status"] for r in in_order(table)] == ["ok", "failed", "failed"]
    assert res.json("best_recipe")["recipe"]["parameters"] == {"step": 0}


@pytest.mark.parametrize("target, last", [(3.3, 3), (3.2, 2), (3.4, 3), (9.0, 4)])
def test_target_ends_the_search_at_the_first_recipe_that_reaches_it(sweep, target, last):
    res, table = stopped(sweep, CLIMB, stopping={"rule": "target", "target": target})
    assert steps(table) == list(range(last + 1))  # reaching the target exactly counts
    assert table["stop_reason"] == "stopping-target"
    assert res.json("best_recipe")["recipe"]["parameters"] == {"step": last}


def test_target_follows_the_direction_and_may_never_be_reached(sweep):
    values = [5.0, 4.0, 4.8, 4.9, 1.0]
    res, table = stopped(sweep, values, direction="minimize", stopping={"rule": "target", "target": 4.5})
    assert steps(table) == [0, 1] and table["stop_reason"] == "stopping-target"
    res, table = stopped(sweep, values, direction="minimize", stopping={"rule": "target", "target": 0.5})
    assert steps(table) == list(range(5)) and table["stop_reason"] == "space-exhausted"
    res, table = stopped(sweep, CLIMB, stopping={"rule": "target", "target": 99.0})
    assert table["stop_reason"] == "space-exhausted"


def test_a_failed_recipe_never_satisfies_a_target(sweep):
    res, table = stopped(sweep, ["fail", "omit", 5.0, 6.0], stopping={"rule": "target", "target": 5.0})
    assert steps(table) == [0, 1, 2] and table["stop_reason"] == "stopping-target"


# -- failures are data ----------------------------------------------------------------------------------------------------
BAD = [4.0, "fail", "omit", 2.0, "norcpt", 3.0]


def test_failing_recipes_are_recorded_unranked_and_do_not_fail_the_job(sweep):
    res = sweep(table_project(BAD), table_config(BAD))
    assert res.ok, res.manifest.error
    table = res.json("evidence_table")
    rows = {r["parameters"]["step"]: r for r in table["recipes"]}
    assert [rows[s]["status"] for s in range(6)] == ["ok", "failed", "failed", "ok", "failed", "ok"]
    # ranked recipes first (best first), then the failed ones in evaluation order
    assert [r["parameters"]["step"] for r in table["recipes"]] == [0, 5, 3, 1, 2, 4]
    assert [r["rank"] for r in table["recipes"]] == [1, 2, 3, None, None, None]
    assert table["counts"] == {"space_size": 6, "evaluated": 6, "usable": 3, "failed": 3}
    exit_error = rows[1]["error"]
    assert exit_error.startswith("exit 3") and "boom at step 1" in exit_error  # the command's stderr is kept
    assert "objective metric 'validation_score'" in rows[2]["error"] and "missing" in rows[2]["error"]
    assert "no receipt.json" in rows[4]["error"]
    for step in (1, 2, 4):
        assert rows[step]["objective"] is None and rows[step]["metrics"] == {}
        assert rows[step]["evaluations"][0]["status"] == "failed"
    assert rows[2]["evaluations"][0]["metrics"] == {"other_metric": 1.0}  # what it did report is kept
    assert res.json("best_recipe")["recipe"]["parameters"] == {"step": 0}
    assert res.receipt["metrics"] == {"recipes_evaluated": 6, "recipes_usable": 3, "recipes_failed": 3,
                                      "best_objective": 4.0}


def test_one_failed_seed_fails_the_whole_recipe(sweep):
    config = make_config(["--out", "{out}", "--case", "{param.case}", "--seed", "{seed}"],
                         {"case": {"values": [0, 1, 2]}}, seeds=[1, 2, 3])
    res = sweep(SEED_FAIL, config)
    assert res.ok, res.manifest.error
    table = res.json("evidence_table")
    rows = {r["parameters"]["case"]: r for r in table["recipes"]}
    assert [e["status"] for e in rows[1]["evaluations"]] == ["ok", "failed", "ok"]  # every seed still ran
    assert rows[1]["status"] == "failed" and rows[1]["rank"] is None and "seed 2 diverged" in rows[1]["error"]
    # case 1 would have been the best of the other seeds' mean (about 1.02) only if it were counted
    assert [r["parameters"]["case"] for r in table["recipes"]] == [2, 0, 1]
    assert res.json("best_recipe")["recipe"]["parameters"] == {"case": 2}


def test_all_recipes_failing_is_a_succeeded_job_with_no_best_recipe(sweep):
    values = ["fail", "omit", "norcpt"]
    res = sweep(table_project(values), table_config(values))
    assert res.ok, res.manifest.error
    best = res.json("best_recipe")
    assert best["status"] == "none" and best["recipe"] is None
    assert best["exploratory"] is True and best["claim"] is None and best["selection_split"] == "visible-validation"
    table = res.json("evidence_table")
    assert table["counts"] == {"space_size": 3, "evaluated": 3, "usable": 0, "failed": 3}
    assert [r["rank"] for r in table["recipes"]] == [None] * 3
    assert res.receipt["best_recipe_id"] is None
    assert res.receipt["metrics"] == {"recipes_evaluated": 3, "recipes_usable": 0, "recipes_failed": 3}


@pytest.mark.parametrize("word", ["nan", "inf"])
def test_a_nonfinite_objective_is_a_failed_recipe_not_a_failed_job(sweep, word):
    values = [4.0, word, 2.0]  # a diverged run reports NaN or infinity
    res = sweep(table_project(values), table_config(values))
    assert res.ok, res.manifest.error
    table = res.json("evidence_table")
    rows = {r["parameters"]["step"]: r for r in table["recipes"]}
    assert rows[1]["status"] == "failed" and rows[1]["rank"] is None and "not finite" in rows[1]["error"]
    assert [r["parameters"]["step"] for r in table["recipes"]] == [0, 2, 1]
    assert res.json("best_recipe")["recipe"]["parameters"] == {"step": 0}


def test_a_nonfinite_side_metric_does_not_fail_the_job(sweep):
    values = [4.0, "sidenan", 2.0]  # the objective is fine; another reported metric is NaN
    res = sweep(table_project(values), table_config(values))
    assert res.ok, res.manifest.error
    table = res.json("evidence_table")
    assert [r["parameters"]["step"] for r in table["recipes"]] == [1, 0, 2]  # 7.0 is the best objective


# -- visible validation only -----------------------------------------------------------------------------------------------
HELDOUT_OBJECTIVES = ["test_accuracy", "Holdout_loss", "heldout_auc", "held_out_f1", "TEST_loss", "test",
                      "accuracy_test", "val.test.loss", "test-accuracy"]


@pytest.mark.parametrize("objective", HELDOUT_OBJECTIVES)
def test_an_objective_named_after_a_held_out_split_is_refused(sweep, objective):
    res = sweep(table_project([1.0]), table_config([1.0], objective=objective))
    refused(res, objective, "held-out", "visible validation only")
    assert not list(Path(res.invocation.scratch_dir).rglob("trial-*"))  # nothing was run
    assert "evidence_table" not in res.slots()


@pytest.mark.parametrize("objective", ["testAccuracy", "holdoutLoss", "heldOutAuc"])
def test_camel_case_held_out_objective_names_are_refused_too(sweep, objective):
    res = sweep(table_project([1.0]), table_config([1.0], objective=objective))
    refused(res, "held-out")


def test_contest_score_and_similar_words_are_not_held_out(sweep):
    for objective in ("contest_score", "latest_loss", "attest_rate", "validation_accuracy"):
        project = TABLE.replace('{"validation_score": v}', '{"' + objective + '": v}').replace("__VALUES__", "[1.0, 2.0]")
        res = sweep(project, table_config([1.0, 2.0], objective=objective))
        assert res.ok, (objective, res.manifest.error)
        best = res.json("best_recipe")
        assert best["status"] == "found" and best["recipe"]["objective"]["metric"] == objective
        assert best["recipe"]["parameters"] == {"step": 1}


def test_a_command_that_reports_a_held_out_metric_is_recorded_as_failed(sweep):
    parameters = {"leak": {"values": ["test_accuracy", "Holdout_loss", "heldout_f1", "contest_score"]}}
    config = make_config(["--out", "{out}", "--leak", "{param.leak}"], parameters)
    res = sweep(LEAKY, config)
    assert res.ok, res.manifest.error
    table = res.json("evidence_table")
    rows = {r["parameters"]["leak"]: r for r in table["recipes"]}
    for leak in ("test_accuracy", "Holdout_loss", "heldout_f1"):
        row = rows[leak]
        assert row["status"] == "failed" and row["rank"] is None and row["objective"] is None
        assert "held-out" in row["error"] and leak in row["error"] and "visible validation only" in row["error"]
        assert "metrics" not in row["evaluations"][0]  # the held-out number is not recorded
    assert rows["contest_score"]["status"] == "ok" and rows["contest_score"]["rank"] == 1
    assert "contest_score" in rows["contest_score"]["metrics"]
    assert res.json("best_recipe")["recipe"]["parameters"] == {"leak": "contest_score"}
    assert "0.8765431" in json.dumps(rows["contest_score"])  # the allowed metric's value is kept
    for leak in ("test_accuracy", "Holdout_loss", "heldout_f1"):
        assert "0.8765431" not in json.dumps(rows[leak])  # a held-out value never reaches the evidence


@pytest.mark.parametrize("port", ["test_inputs", "test", "holdout", "heldout_labels", "labels"])
def test_a_test_or_held_out_input_port_is_refused(sweep, port):
    inputs = {port: (b"npz", "application/x-npz")}
    res = sweep(table_project([1.0]), table_config([1.0]), inputs=inputs)
    refused(res, "undeclared input ports", port)
    assert not list(Path(res.invocation.scratch_dir).rglob("trial-*"))
    assert "evidence_table" not in res.slots()


def test_only_the_declared_ports_are_staged_and_each_run_has_a_private_work_dir(sweep):
    inputs = {"train": (b"train-bytes", "application/x-npz"), "validation": (b"val-bytes", "application/x-npz"),
              "task": ({"task_id": "t"}, "application/json")}
    config = make_config(["--out", "{out}"], {"x": {"values": [1, 2]}}, seeds=[1, 2])
    res = sweep(STAGING, config, inputs=inputs)
    assert res.ok, res.manifest.error
    evaluations = [e for r in res.json("evidence_table")["recipes"] for e in r["evaluations"]]
    assert len(evaluations) == 4
    for e in evaluations:
        m = e["metrics"]
        assert (m["has_train_npz"], m["has_validation_npz"], m["has_task_json"]) == (1.0, 1.0, 1.0)
        assert m["has_tr_bin"] == 0.0
        assert m["saw_leak"] == 0.0  # no evaluation sees another's files
    # a file name map overrides the default <port><ext> name; the name must be a plain one
    config["command"]["files"] = {"train": "tr.bin"}
    res = sweep(STAGING, config, inputs=inputs)
    for r in res.json("evidence_table")["recipes"]:
        m = r["evaluations"][0]["metrics"]
        assert (m["has_tr_bin"], m["has_train_npz"], m["has_validation_npz"]) == (1.0, 0.0, 1.0)
    config["command"]["files"] = {"train": "../escape.bin"}
    refused(sweep(STAGING, config, inputs=inputs), "plain name")


HELDOUT_SEEN = textwrap.dedent(
    """
    import json, os, sys
    a = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    m = {"has_project": float(os.path.exists("project.py")), "has_notes": float(os.path.exists("notes.txt")),
         "leaked_csv": float(os.path.exists("test.csv")), "leaked_json": float(os.path.exists("test.json"))}
    m["validation_score"] = 1.0
    json.dump(m, open(a["--out"] + "/receipt.json", "w"))
    """
)


def project_with_heldout(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "project.py").write_text(HELDOUT_SEEN)
    for name in ("test.csv", "test.json", "notes.txt"):
        (project / name).write_text("id,label\n1,secret-label\n")
    return project


def test_without_a_source_bundle_nothing_is_copied_unless_it_is_listed(sweep, tmp_path):
    refused(sweep(None, make_config(["--out", "{out}"], {"x": {"values": [1]}}), cwd=project_with_heldout(tmp_path)),
            "command.copy must list the project files")


def test_only_listed_files_reach_an_evaluation_and_test_files_stay_out(sweep, tmp_path):
    project = project_with_heldout(tmp_path)
    config = make_config(["--out", "{out}"], {"x": {"values": [1]}})
    config["command"]["copy"] = ["project.py", "notes.txt"]
    res = sweep(None, config, cwd=project)
    assert res.ok, res.manifest.error
    m = res.json("evidence_table")["recipes"][0]["evaluations"][0]["metrics"]
    assert (m["has_project"], m["has_notes"], m["leaked_csv"], m["leaked_json"]) == (1.0, 1.0, 0.0, 0.0)


@pytest.mark.parametrize("name", ["test.csv", "test.json", "heldout.npz", "hold_out.csv", "test_set.csv", "testset.csv",
                                  "test1.csv", "testfeatures.csv", "x_test2.npy", "TestData.csv", "holdout3.npz"])
def test_listing_a_held_out_file_is_refused(sweep, tmp_path, name):
    project = project_with_heldout(tmp_path)
    (project / name).write_text("x")
    config = make_config(["--out", "{out}"], {"x": {"values": [1]}})
    config["command"]["copy"] = ["project.py", name]
    refused(sweep(None, config, cwd=project), "named after a held-out split")


@pytest.mark.parametrize("entry, fragment", [("missing.py", "not a regular file"), ("../up.py", "plain file name"),
                                              (".hidden", "plain file name"), (7, "plain file name")])
def test_a_list_entry_must_be_a_regular_project_file(sweep, tmp_path, entry, fragment):
    config = make_config(["--out", "{out}"], {"x": {"values": [1]}})
    config["command"]["copy"] = ["project.py", entry]
    refused(sweep(None, config, cwd=project_with_heldout(tmp_path)), fragment)


def test_a_listed_symlink_is_refused(sweep, tmp_path):
    project = project_with_heldout(tmp_path)
    (project / "link.txt").symlink_to(project / "test.csv")
    config = make_config(["--out", "{out}"], {"x": {"values": [1]}})
    config["command"]["copy"] = ["project.py", "link.txt"]
    refused(sweep(None, config, cwd=project), "not a regular file")


@pytest.mark.parametrize("path", ["test.csv", "data/heldout.json", "holdout/x.npz",
                                  "test1.csv", "data/testfeatures.csv", "test2/x.npz"])
def test_a_source_bundle_cannot_carry_held_out_data(sweep, path):
    res = sweep(None, table_config([1.0]), files={"project.py": table_project([1.0]), path: "1,2"})
    refused(res, "named after a held-out split")


def test_a_source_bundle_may_carry_code_named_after_tests(sweep):
    res = sweep(None, table_config([1.0]), files={"project.py": table_project([1.0]), "test_helpers.py": "x = 1"})
    assert res.ok, res.manifest.error


@pytest.mark.parametrize("path", ["test_helpers.py", "testfeatures.py", "x_test2.py", "tests/conftest.py", "test2/util.py",
                                  "holdout3.md", "heldout_notes.toml", "run_test1.sh"])
def test_the_code_suffix_exemption_still_lets_test_named_source_files_into_a_bundle(sweep, path):
    """Code, notes and config may carry a test-like name (the data guard is about data files); the file is bundled."""
    res = sweep(None, table_config([1.0]), files={"project.py": table_project([1.0]), path: "x = 1"})
    assert res.ok, res.manifest.error


@pytest.mark.parametrize("path", ["latest.csv", "data/contest.csv", "attested.json", "protest/x.npz", "greatest_hits.npy"])
def test_a_source_bundle_may_carry_data_whose_name_merely_contains_test(sweep, path):
    res = sweep(None, table_config([1.0]), files={"project.py": table_project([1.0]), path: "1,2"})
    assert res.ok, res.manifest.error


@pytest.mark.parametrize("name", ["test1.csv", "testfeatures.csv", "x_test2.npy", "TestData.csv", "holdout3.npz", "heldout1.json",
                                  "myTestSplit2.csv", "test.csv", "hold_out.csv"])
def test_a_data_file_name_with_a_word_beginning_test_holdout_or_heldout_is_held_out(name):
    assert load_sweep_module()._names_heldout_file(name)


@pytest.mark.parametrize("name", ["latest.csv", "contest.csv", "attested.json", "protest.npz", "data.csv", "greatest_hits.csv"])
def test_a_data_file_name_that_merely_contains_test_is_not_held_out(name):
    assert not load_sweep_module()._names_heldout_file(name)


COPY_SEEN = textwrap.dedent(
    """
    import json, os, sys
    a = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    names = ("latest.csv", "contest.csv", "attested.csv", "test1.csv", "testfeatures.csv", "x_test2.npy", "TestData.csv")
    m = {"seen_%d" % i: float(os.path.exists(n)) for i, n in enumerate(names)}  # metric names must not read as held-out
    m["validation_score"] = 1.0
    json.dump(m, open(a["--out"] + "/receipt.json", "w"))
    """
)


def test_benign_names_that_merely_contain_test_are_copied_and_test_like_files_stay_out(sweep, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "project.py").write_text(COPY_SEEN)
    for name in ("latest.csv", "contest.csv", "attested.csv", "test1.csv", "testfeatures.csv", "x_test2.npy", "TestData.csv"):
        (project / name).write_text("1,2\n")
    config = make_config(["--out", "{out}"], {"x": {"values": [1]}})
    config["command"]["copy"] = ["project.py", "latest.csv", "contest.csv", "attested.csv"]
    res = sweep(None, config, cwd=project)
    assert res.ok, res.manifest.error
    m = res.json("evidence_table")["recipes"][0]["evaluations"][0]["metrics"]
    assert [m["seen_%d" % i] for i in range(7)] == [1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0]  # the three benign files only


# -- every evaluation is its own process group: stopped and reaped on timeout, stop request and cancellation ------------
FORKS = textwrap.dedent(
    """
    import json, os, signal, subprocess, sys, time
    a = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    worker = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"
    child = subprocess.Popen([sys.executable, "-c", worker], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    open(a["--pids"], "w").write(str(child.pid))
    if a["--mode"] == "hang":
        time.sleep(120)
    json.dump({"validation_score": 1.0}, open(a["--out"] + "/receipt.json", "w"))
    """
)


def alive(pid):
    """Running, not a zombie: a container's init need not reap the worker the test orphaned."""
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return out.returncode == 0 and bool(out.stdout.strip()) and not out.stdout.strip().startswith("Z")


def fork_config(pids, mode, **command):
    config = make_config(["--out", "{out}", "--pids", str(pids), "--mode", mode], {"x": {"values": [1]}})
    config["command"].update(command)
    return config


def wait_for(predicate, seconds=20.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_timeout_kills_the_forked_workers_of_an_evaluation(sweep, tmp_path):
    pids = tmp_path / "pid"
    res = sweep(FORKS, fork_config(pids, "hang", timeout_seconds=1))
    assert res.ok, res.manifest.error
    assert res.json("evidence_table")["recipes"][0]["evaluations"][0]["status"] == "timeout"
    assert not alive(int(pids.read_text()))  # it ignored SIGTERM: the group got SIGKILL too


def test_a_normal_exit_leaves_no_worker_behind(sweep, tmp_path):
    pids = tmp_path / "pid"
    res = sweep(FORKS, fork_config(pids, "exit"))
    assert res.ok, res.manifest.error
    assert res.json("evidence_table")["recipes"][0]["status"] == "ok"
    assert not alive(int(pids.read_text()))


def start_worker(tmp_path, config):
    root = tmp_path / "worker"
    inv = make_invocation(root, ENTRY, inputs={"source": {"files": {"project.py": FORKS}}}, config=config,
                          source_dir=COMPONENTS / NAME, component_id=NAME)
    path = root / "invocation.json"
    inv.write(path)
    return inv, subprocess.Popen([sys.executable, "-m", "eidos_worker.run", str(path)], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)


def test_sigterm_to_the_worker_stops_and_reaps_the_evaluation_group(tmp_path):
    pids = tmp_path / "pid"
    inv, worker = start_worker(tmp_path, fork_config(pids, "hang"))
    try:
        assert wait_for(lambda: pids.exists() and pids.read_text().strip()), "the evaluation did not start"
        child = int(pids.read_text())
        assert alive(child)
        os.killpg(worker.pid, signal.SIGTERM)  # how the launcher cancels: the worker's group, not the evaluation's
        assert worker.wait(timeout=20) == -signal.SIGTERM
        assert wait_for(lambda: not alive(child)), "the evaluation's worker outlived the cancelled job"
    finally:
        worker.kill()
        worker.wait()


def test_the_runners_stop_request_stops_the_evaluation_and_ends_the_search(tmp_path):
    from eidos_worker.context import STOP_FILE
    pids = tmp_path / "pid"
    inv, worker = start_worker(tmp_path, fork_config(pids, "hang"))
    try:
        assert wait_for(lambda: pids.exists() and pids.read_text().strip()), "the evaluation did not start"
        child = int(pids.read_text())
        Path(inv.scratch_dir, STOP_FILE).write_text("stop")
        assert worker.wait(timeout=30) == 0
        assert not alive(child)
    finally:
        worker.kill()
        worker.wait()
    result = Result(OutputManifest.read(inv.staging_dir), inv, "")
    assert result.ok, result.manifest.error
    table = result.json("evidence_table")
    assert table["stop_reason"] == "stop-requested" and table["counts"]["usable"] == 0
    assert table["recipes"][0]["evaluations"][0]["status"] == "cancelled" and table["recipes"][0]["rank"] is None


# -- planted secrets: an evaluation does not inherit them, and failure text never carries them ----------------------------
PLANTED = "sk-planted-0123456789abcdefGHIJ"
LEAKS = textwrap.dedent(
    """
    import os, sys
    a = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    sys.stderr.write("env=" + os.environ.get("PLANTED_API_TOKEN", "<absent>") + "\\n")
    sys.stderr.write("HOME=" + os.environ.get("HOME", "") + "\\n")
    if a["--file"] != "none":
        sys.stderr.write("from-file=" + open(a["--file"]).read() + "\\n")
    sys.exit(1)
    """
)


def secret_free(res):
    text = json.dumps([res.json("evidence_table"), res.json("best_recipe"), res.receipt])
    return PLANTED not in text and PLANTED[:12] not in text, text


def leak_config(path="none"):
    return make_config(["--out", "{out}", "--file", str(path)], {"x": {"values": [1]}})


def test_an_evaluation_does_not_inherit_the_workers_secret(sweep, monkeypatch):
    monkeypatch.setenv("PLANTED_API_TOKEN", PLANTED)
    res = sweep(LEAKS, leak_config())
    assert res.ok, res.manifest.error
    error = res.json("evidence_table")["recipes"][0]["evaluations"][0]["error"]
    assert "env=<absent>" in error and "exit 1" in error
    assert secret_free(res)[0]


def test_a_secret_a_command_printed_anyway_is_redacted_before_publishing(sweep, monkeypatch, tmp_path):
    monkeypatch.setenv("PLANTED_API_TOKEN", PLANTED)
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text(PLANTED)
    res = sweep(LEAKS, leak_config(secret_file))
    assert res.ok, res.manifest.error
    assert secret_free(res)[0]
    assert "from-file=[REDACTED]" in res.json("evidence_table")["recipes"][0]["evaluations"][0]["error"]


def load_sweep_module():
    spec = importlib.util.spec_from_file_location("recipe_sweep_under_test", COMPONENTS / NAME / "recipe_sweep.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- every evaluation has its own directory, and a work directory that cannot be prepared costs one evaluation ----------------
def unit_config():
    return {"command": {"argv": ["python", "project.py", *FORMULA_ARGV]},
            "objective": {"metric": "validation_score", "direction": "maximize"}}


UNIT_FILES = {"project.py": FORMULA}
UNIT_PARAMS = [{"lr": 0.2, "depth": 1}, {"lr": 0.1, "depth": 2}]  # the first is the formula's optimum


def unit_evaluate(module, root, index, seed=11, fold=0, parameters=None):
    return module._evaluate(root, index, seed, fold, 1, parameters or UNIT_PARAMS[0], unit_config(), {}, None, UNIT_FILES,
                            30, [], lambda: False)


def unit_search(module, root, *, seeds=(11,)):
    return module._search(list(UNIT_PARAMS), len(UNIT_PARAMS), unit_config(), inputs={}, reader=None, source_files=UNIT_FILES,
                          seeds=list(seeds), folds=1, budget={"max_trials": 10}, stopping={"rule": "none"},
                          context=SimpleNamespace(stop_requested=lambda: False), root=root, secrets=[], started=time.time())


@pytest.mark.parametrize("index", [0, "screen-0003"], ids=["numbered-recipe", "stage-labelled-recipe"])
def test_evaluating_the_same_recipe_seed_and_fold_twice_gives_each_its_own_directory(tmp_path, index):
    module, root = load_sweep_module(), tmp_path / "sweep"
    label = index if isinstance(index, str) else "%04d" % index
    first = unit_evaluate(module, root, index)
    first_dir = root / ("trial-%s-s11-f0" % label)
    (first_dir / "out" / "marker.txt").write_text("first")  # a second evaluation must not reuse or clear this directory
    second = unit_evaluate(module, root, index)
    third = unit_evaluate(module, root, index)
    want = formula(0.2, 1, 11, 0)
    for record in (first, second, third):
        assert record["status"] == "ok" and (record["seed"], record["fold"]) == (11, 0)
        assert record["metrics"]["validation_score"] == approx(want)
    assert sorted(d.name for d in root.iterdir()) == sorted(
        "trial-%s-s11-f0%s" % (label, suffix) for suffix in ("", "-r2", "-r3"))
    assert (first_dir / "out" / "marker.txt").read_text() == "first"
    for d in root.iterdir():  # each ran in its own work directory and wrote its own receipt
        assert (d / "out" / "receipt.json").is_file() and (d / "work" / "project.py").is_file()
        assert (d / "out" / "marker.txt").exists() == (d == first_dir)


def test_a_different_seed_or_fold_does_not_get_a_retry_suffix(tmp_path):
    module, root = load_sweep_module(), tmp_path / "sweep"
    for seed, fold in [(11, 0), (11, 1), (12, 0)]:
        assert unit_evaluate(module, root, 0, seed=seed, fold=fold)["status"] == "ok"
    assert sorted(d.name for d in root.iterdir()) == ["trial-0000-s11-f0", "trial-0000-s11-f1", "trial-0000-s12-f0"]


def failing_second_preparation(module, monkeypatch, *, fail_on=2):
    """Make the fail_on-th preparation of a working directory raise OSError, as a full disk would."""
    real, calls = module._prepare_work, {"n": 0}

    def prepare(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == fail_on:
            raise OSError(28, "No space left on device")
        return real(*args, **kwargs)

    monkeypatch.setattr(module, "_prepare_work", prepare)
    return calls


def test_an_os_error_preparing_the_work_directory_is_a_failed_evaluation_not_an_aborted_sweep(tmp_path, monkeypatch):
    module = load_sweep_module()
    failing_second_preparation(module, monkeypatch)
    rows, stop_reason = unit_search(module, tmp_path / "sweep")
    assert stop_reason == "space-exhausted" and [r["index"] for r in rows] == [0, 1]
    first, second = rows
    assert first["status"] == "ok" and first["evaluations"][0]["status"] == "ok"
    assert first["objective"]["mean"] == approx(formula(0.2, 1, 11, 0))  # the evidence gathered before the failure is kept
    (evaluation,) = second["evaluations"]
    assert second["status"] == "failed" and evaluation["status"] == "failed"
    assert (evaluation["seed"], evaluation["fold"]) == (11, 0)
    assert "could not prepare the working directory" in evaluation["error"] and "No space left on device" in evaluation["error"]
    assert second["error"] == evaluation["error"] and second["rank"] is None and "metrics" not in evaluation
    ranked = module._ranked(rows, "maximize")  # the failed recipe is recorded unranked, the first still wins
    assert [(r["index"], r["rank"]) for r in ranked] == [(0, 1), (1, None)]
    assert module._evidence_digest(rows).startswith("sha256:")


def test_a_work_directory_failure_between_seeds_fails_that_recipe_and_the_sweep_goes_on(tmp_path, monkeypatch):
    module = load_sweep_module()
    failing_second_preparation(module, monkeypatch)
    rows, stop_reason = unit_search(module, tmp_path / "sweep", seeds=(1, 2, 3))
    assert stop_reason == "space-exhausted" and [r["status"] for r in rows] == ["failed", "ok"]
    assert [e["status"] for e in rows[0]["evaluations"]] == ["ok", "failed", "ok"]  # the other seeds still ran
    assert [e["status"] for e in rows[1]["evaluations"]] == ["ok"] * 3
    assert "could not prepare the working directory" in rows[0]["error"]


def test_a_work_directory_that_cannot_be_built_fails_every_recipe_but_the_job_still_succeeds(sweep):
    """End to end: 'a' is a file in the bundle and 'a/b' needs it to be a directory, so no work directory can be made."""
    files = {"project.py": table_project([1.0, 2.0]), "a": "x", "a/b": "y"}
    res = sweep(None, table_config([1.0, 2.0]), files=files)
    assert res.ok, res.manifest.error
    table = res.json("evidence_table")
    assert table["counts"] == {"space_size": 2, "evaluated": 2, "usable": 0, "failed": 2}
    for row in table["recipes"]:
        assert row["status"] == "failed" and "could not prepare the working directory" in row["evaluations"][0]["error"]
    assert res.json("best_recipe")["status"] == "none"


@pytest.mark.parametrize("line", [
    "Authorization: Bearer abcdefgh12345678", "api_key=abcd1234efgh", 'OPENAI_API_KEY="abcd1234efgh"', "password: hunter2hunter2",
    "ghp_abcdefghijklmnopqrstuvwxyz012345", "sk-abcdefghijklmnop", "AKIAABCDEFGHIJKLMNOP", "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.c2lnbmF0dXJl",
    "tok " + "A1b2C3d4" * 6])
def test_credential_shapes_are_redacted(line):
    assert load_sweep_module()._redact(line, []).count("[REDACTED]") >= 1
    secret_body = line.split()[-1].split("=")[-1].strip('"')
    assert secret_body not in load_sweep_module()._redact(line, [])


def test_ordinary_errors_stay_readable():
    text = "Traceback (most recent call last):\n  File \"/tmp/w/project.py\", line 3\nValueError: lr must be positive"
    assert load_sweep_module()._redact(text, []) == text


def test_redaction_happens_before_the_tail_is_cut(tmp_path):
    sweep_module = load_sweep_module()
    secret = "planted-" + "x7" * 20
    # the tail cut would otherwise land inside the secret and publish its end
    text = secret + "y" * (sweep_module.TAIL_CHARS - 5)
    path = tmp_path / "stderr.log"
    path.write_text(text)
    tail = sweep_module._stderr_tail(path, [secret])
    assert "x7x7" not in tail and len(tail) <= sweep_module.TAIL_CHARS


def test_the_standalone_toolkit_path_does_not_leak_a_planted_secret(toolkit, tmp_path):
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({"files": {"project.py": LEAKS}}))
    out = tmp_path / "toolkit-secret"
    spec = {"inputs": {"source": {"file": str(bundle), "media_type": "application/json"}}, "config": leak_config()}
    proc = run_toolkit(toolkit, tmp_path, NAME, "--args", json.dumps(spec), "--out", str(out),
                       env={"PLANTED_API_TOKEN": PLANTED})
    assert proc.returncode == 0, proc.stdout[-1500:] + proc.stderr[-1500:]
    staging = out / "staging"
    manifest = json.loads((staging / "eidos-output-manifest.json").read_text())
    published = json.dumps(manifest) + "".join((staging / o["filename"]).read_text() for o in manifest["outputs"])
    assert "env=<absent>" in published and PLANTED not in published


@pytest.mark.parametrize("path", ["../evil.py", "/abs/evil.py", "a/../../evil.py"])
def test_source_bundle_paths_cannot_escape_the_work_dir(sweep, path):
    res = sweep(None, table_config([1.0]), files={"project.py": table_project([1.0]), path: "x = 1"})
    refused(res, "escapes the working directory")


# -- placeholders and the recipe template ------------------------------------------------------------------------------------
@pytest.mark.parametrize("arg, shown", [("{param.typo}", "{param.typo}"), ("{nonsense}", "{nonsense}"),
                                         ("--x={seed}{nope}", "{nope}"), ("{Seed}", "{Seed}"),
                                         ("{recipe}", "{recipe}")])
def test_unknown_placeholders_fail_with_a_clear_error(sweep, arg, shown):
    config = table_config([1.0])
    config["command"]["argv"].append(arg)  # {recipe} is unknown unless config.recipe is set
    res = sweep(table_project([1.0]), config)
    message = refused(res, "unknown placeholders", shown)
    assert "argv[" in message  # says which argument
    assert "evidence_table" not in res.slots()


def test_braces_that_are_not_placeholders_pass_through_and_values_are_formatted_for_the_command(sweep):
    parameters = {"lr": {"values": [0.001]}, "flag": {"values": [True]}, "name": {"values": ["adam"]},
                  "dims": {"values": [[1, 2]]}}
    argv = ["--out", "{out}", "--lr", "{param.lr}", "--flag", "{param.flag}", "--name", "{param.name}",
            "--dims", "{param.dims}", "--cfg", '{"k": 1}']
    res = sweep(FORMATS, make_config(argv, parameters))
    assert res.ok, res.manifest.error
    (row,) = res.json("evidence_table")["recipes"]
    assert row["status"] == "ok", row.get("error")
    assert row["objective"]["mean"] == 1.0  # true as true, strings raw, numbers and lists as JSON


TEMPLATE = {"optimizer": {"name": "sgd", "lr": {"$param": "lr"}}, "layers": [{"$param": "width"}, {"$param": "width"}],
            "tag": "fixed", "mode": {"$param": "mode"}}


def test_recipe_template_renders_params_into_the_recipe_file(sweep):
    parameters = {"lr": {"values": [0.1, 0.01]}, "width": {"values": [8]}, "mode": {"values": ["a"]}}
    config = make_config(["--out", "{out}", "--recipe", "{recipe}"], parameters, recipe=TEMPLATE)
    res = sweep(RECIPE, config)
    assert res.ok, res.manifest.error
    seen = sorted((json.loads(p.read_text()) for p in Path(res.invocation.scratch_dir).rglob("recipe_seen.json")),
                  key=lambda r: r["optimizer"]["lr"])
    assert seen == [
        {"optimizer": {"name": "sgd", "lr": 0.01}, "layers": [8, 8], "tag": "fixed", "mode": "a"},
        {"optimizer": {"name": "sgd", "lr": 0.1}, "layers": [8, 8], "tag": "fixed", "mode": "a"},
    ]  # numbers stay numbers, the template's other keys are kept
    table, best = res.json("evidence_table"), res.json("best_recipe")
    assert [r["parameters"]["lr"] for r in table["recipes"]] == [0.1, 0.01]
    assert best["recipe"]["parameters"] == {"lr": 0.1, "mode": "a", "width": 8}
    assert best["recipe"]["recipe_template"] == TEMPLATE  # the template, not one rendering of it
    assert best["recipe"]["evaluations"][0]["metrics"] == {"validation_score": 1.0, "n_layers": 2.0}


def test_an_unknown_param_in_the_recipe_template_is_refused(sweep):
    template = {"optimizer": {"lr": {"$param": "nope"}}}
    config = make_config(["--out", "{out}", "--recipe", "{recipe}"], {"lr": {"values": [0.1]}}, recipe=template)
    res = sweep(RECIPE, config)
    refused(res, "unknown parameter 'nope'")
    assert "evidence_table" not in res.slots()


# -- refusals of an unusable configuration ------------------------------------------------------------------------------------
def _set(path, value):
    def apply(config):
        node = config
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
    return apply


def _random_param(spec, **space):
    def apply(config):
        config["space"] = {"mode": "random", "seed": 1, "parameters": {"step": spec}, **space}
    return apply


REFUSALS = [
    ("argv-too-short", _set(["command", "argv"], ["python"]), "at least two"),
    ("no-objective-metric", _set(["objective"], {}), "objective.metric"),
    ("bad-direction", _set(["objective", "direction"], "up"), "direction"),
    ("unknown-rule", _set(["stopping"], {"rule": "eventually"}), "stopping.rule"),
    ("patience-zero", _set(["stopping"], {"rule": "patience", "patience": 0}), "stopping.patience"),
    ("target-missing", _set(["stopping"], {"rule": "target"}), "stopping.target"),
    ("empty-seeds", _set(["seed_policy"], {"seeds": []}), "seed_policy.seeds"),
    ("boolean-seed", _set(["seed_policy"], {"seeds": [True]}), "seed_policy.seeds"),
    ("string-seed", _set(["seed_policy"], {"seeds": ["1"]}), "seed_policy.seeds"),
    ("zero-folds", _set(["seed_policy"], {"folds": 0}), "seed_policy.folds"),
    ("unknown-mode", _set(["space", "mode"], "bayes"), "grid or random"),
    ("no-parameters", _set(["space", "parameters"], {}), "at least one parameter"),
    ("bad-parameter-name", _set(["space", "parameters"], {"bad-name": {"values": [1]}}), "must match"),
    ("grid-without-values", _set(["space", "parameters"], {"step": {"low": 0, "high": 1}}), "non-empty values"),
    ("random-without-range", _random_param({}), "needs values, or low and high"),
    ("log-from-zero", _random_param({"low": 0, "high": 1, "log": True}), "low > 0"),
    ("low-above-high", _random_param({"low": 2, "high": 1}), "low above high"),
]


@pytest.mark.parametrize("seeds", [[11, 11], [3, 5, 3], [0, 0]])
def test_a_repeated_seed_in_the_seed_policy_is_refused_before_anything_runs(sweep, seeds):
    """A repeated seed re-measures the same thing and would count it twice in the mean and the standard deviation."""
    res = sweep(table_project([1.0, 2.0]), table_config([1.0, 2.0], seeds=seeds))
    refused(res, "seed_policy.seeds", "must not repeat a seed")
    assert not list(Path(res.invocation.scratch_dir).rglob("trial-*"))
    assert "evidence_table" not in res.slots() and "best_recipe" not in res.slots()


@pytest.mark.parametrize("mutate, fragment", [(m, f) for _, m, f in REFUSALS], ids=[r[0] for r in REFUSALS])
def test_an_unusable_configuration_is_refused_before_anything_runs(sweep, mutate, fragment):
    config = table_config([1.0, 2.0])
    mutate(config)
    res = sweep(table_project([1.0, 2.0]), config)
    refused(res, fragment)
    assert not list(Path(res.invocation.scratch_dir).rglob("trial-*"))
    assert "evidence_table" not in res.slots()


# -- the standalone toolkit (no lab) ------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def toolkit(tmp_path_factory):
    sys.path.insert(0, str(ROOT))
    try:
        from study.toolkit.build_toolkit import build_toolkit
    finally:
        sys.path.remove(str(ROOT))
    return build_toolkit(tmp_path_factory.mktemp("kit"))


def bare_python():
    """An interpreter that can import the standard library and nothing else: not the lab, not this venv."""
    candidate = Path(sys.base_prefix) / "bin" / f"python{sys.version_info[0]}.{sys.version_info[1]}"
    return str(candidate) if candidate.exists() else sys.executable


def run_toolkit(toolkit, tmp_path, *args, env=None):
    """The copied run_component.py under a bare interpreter, with no PYTHONPATH and no lab on the path."""
    return subprocess.run([bare_python(), "-I", str(toolkit / "run_component.py"), *args], capture_output=True,
                          text=True, timeout=240, cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin", **(env or {})})


def test_toolkit_contains_the_component_and_is_listed_in_its_coverage(toolkit):
    sources = COMPONENTS / NAME
    for path in sorted(p for p in sources.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        copy = toolkit / "components" / NAME / path.relative_to(sources)
        assert copy.read_bytes() == path.read_bytes(), copy
    manifest = json.loads((toolkit / "manifest.json").read_text())["sha256"]
    for key in ("eidos-component.json", "recipe_sweep.py", "method-card.md", "fixtures/grid.json"):
        assert f"components/{NAME}/{key}" in manifest
    coverage = (toolkit / "COVERAGE.md").read_text()
    assert NAME in coverage


def test_toolkit_lists_the_component_and_ships_no_lab_package(toolkit, tmp_path):
    listed = run_toolkit(toolkit, tmp_path, "--list")
    assert listed.returncode == 0, listed.stderr
    assert NAME in listed.stdout.split()
    # the interpreter running the toolkit here cannot import the lab, so a pass proves the lab is not needed
    probe = subprocess.run([bare_python(), "-I", "-c", "import eidos"], capture_output=True, text=True)
    assert probe.returncode != 0 and "No module named 'eidos'" in probe.stderr
    files = list(json.loads((toolkit / "manifest.json").read_text())["sha256"])
    for key in files:
        parts = Path(key).parts
        assert not key.startswith("src/"), key
        if "eidos" in parts[:-1]:  # a package directory named eidos: only the exported pure rules may be one
            assert key.startswith("support/eidos/"), key
    assert (toolkit / "support" / "eidos" / "domain").is_dir()
    assert not (toolkit / "src").exists()


def test_toolkit_runs_the_sweep_on_a_source_file_without_the_lab(toolkit, grid, tmp_path):
    lab, config = grid
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({"files": {"project.py": FORMULA}}))
    out = tmp_path / "toolkit-out"
    spec = {"inputs": {"source": {"file": str(bundle), "media_type": "application/json"}}, "config": config}
    proc = run_toolkit(toolkit, tmp_path, NAME, "--args", json.dumps(spec), "--out", str(out))
    assert proc.returncode == 0, proc.stdout[-1500:] + proc.stderr[-1500:]
    staging = out / "staging"
    manifest = json.loads((staging / "eidos-output-manifest.json").read_text())
    assert manifest["status"] == "succeeded", manifest["error"]
    files = {o["slot"]: staging / o["filename"] for o in manifest["outputs"]}
    table, best = (json.loads(files[s].read_text()) for s in ("evidence_table", "best_recipe"))
    assert_grid_documents(table, best)
    assert table["exploratory"] is True and table["claim"] is None
    assert best["recipe"]["parameters"] == {"depth": 1, "lr": 0.2}
    # the same deterministic project gives the same evidence through the lab's runner and the standalone one
    assert manifest["receipt"]["evidence_digest"] == lab.receipt["evidence_digest"] == recomputed_digest(table)
    assert manifest["receipt"]["best_recipe_id"] == best["recipe"]["recipe_id"]
    assert manifest["receipt"]["stop_reason"] == "space-exhausted"
    # the toolkit's default job seed (11) is the sweep's seed when the config names none
    spec["config"] = formula_config(seeds=None, folds=None, budget={"max_trials": 1})
    out2 = tmp_path / "toolkit-out-2"
    proc = run_toolkit(toolkit, tmp_path, NAME, "--args", json.dumps(spec), "--out", str(out2))
    assert proc.returncode == 0, proc.stdout[-1500:] + proc.stderr[-1500:]
    manifest = json.loads((out2 / "staging" / "eidos-output-manifest.json").read_text())
    entry = next(o for o in manifest["outputs"] if o["slot"] == "evidence_table")
    assert json.loads((out2 / "staging" / entry["filename"]).read_text())["seed_policy"] == {"seeds": [11], "folds": 1}
