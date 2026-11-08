#!/usr/bin/env python3
"""MPS vs CPU smoke of the eidos-pytorch training path on the switch-order example (JSON receipt on stdout).

Run in a plain venv that has torch and numpy (no lab needed):
    python scripts/qualify/mps_smoke.py [--steps 45] [--seed 41] [--builder OrderedSwitchNet] [--out receipt.json]

For each of cpu and mps (mps is skipped and reported unavailable where `torch.backends.mps.is_available()` is false, e.g. Linux)
a fresh subprocess runs `eidos_pytorch.training.train_loop` with the same seed and config, with PYTORCH_ENABLE_MPS_FALLBACK=1 and all
warnings recorded, then evaluates accuracy on one fixed 4000-pair draw. Records per device: torch version, availability,
the device the parameters actually lived on, wall time, final training loss, evaluation accuracy and a parameter checksum.
Comparison tolerance (exact equality is not expected across devices; kernels and reduction order differ):
    |final_loss_cpu - final_loss_mps| <= --loss-tol (default 5e-3)   and   |acc_cpu - acc_mps| <= --acc-tol (default 0.02).
`match_within_tolerance` is null when MPS is unavailable. CPU fallbacks are listed from warnings and from stderr text mentioning "fall back".
The train loop moves the model to the device; this script moves each batch (the example adapter builds batches on the CPU).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import warnings
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _paths() -> None:
    for p in (*(REPO / "packages").glob("*/src"), REPO / "examples" / "switch-order-project"):
        if p.is_dir() and str(p) not in sys.path:
            sys.path.append(str(p))


def child(device: str, steps: int, seed: int, builder: str, deterministic: bool) -> dict:
    _paths()
    import dataclasses

    import eidos_adapter as ea
    import torch

    from eidos_pytorch.training import train_loop

    out: dict = {"device_requested": device, "torch": torch.__version__}
    if device == "mps" and not torch.backends.mps.is_available():
        return {**out, "available": False}
    out["available"] = True
    adapter = ea.ADAPTER
    cfg = {"builder": builder, "seed": seed}
    stream = adapter.train_batches({**cfg, "batch_size": 100})

    def to_dev(b):
        return dataclasses.replace(b, **{f.name: getattr(b, f.name).to(device) for f in dataclasses.fields(b)})

    captured: dict = {}
    real_build = adapter.build

    def build(config):  # capture the model to inspect its device and parameters afterwards
        m = real_build(config)
        captured["model"] = m
        return m

    adapter.build = build
    used_det = deterministic
    t0 = time.perf_counter()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            res = train_loop(adapter, cfg, lambda i: to_dev(stream(i)), steps=steps, seed=seed, deterministic=deterministic,
                             device=device)
        except RuntimeError as exc:
            if not deterministic:
                raise
            out["deterministic_error"] = str(exc)[:300]
            used_det = False
            t0 = time.perf_counter()
            res = train_loop(adapter, cfg, lambda i: to_dev(stream(i)), steps=steps, seed=seed, deterministic=False,
                             device=device)
        wall = time.perf_counter() - t0
        model = captured["model"]
        model.eval()
        with torch.no_grad():
            batch = adapter.examples({"split": "test", "data_seed": 76102, "pairs": 4000})
            logits = model(batch.events.to(device), batch.recorder.to(device))
            acc = float(((logits > 0).float().cpu() == batch.target).float().mean())
        params = [p.detach().cpu().double() for p in model.parameters()]
    out.update(
        device_actual=str(next(model.parameters()).device), wall_seconds=round(wall, 4),
        final_loss=res.receipt.get("final_loss"), eval_accuracy=acc, steps=res.receipt.get("end_step"),
        param_checksum=float(sum(p.sum() for p in params)), param_l2=float(sum((p * p).sum() for p in params) ** 0.5),
        deterministic_used=used_det, warnings=sorted({f"{w.category.__name__}: {w.message}" for w in caught})[:20],
        params=[p.flatten().tolist() for p in params] if os.environ.get("MPS_SMOKE_PARAMS") else None)
    return out


def run_device(device: str, args) -> dict:
    env = {**os.environ, "PYTORCH_ENABLE_MPS_FALLBACK": "1"}
    cmd = [sys.executable, __file__, "--child", device, "--steps", str(args.steps), "--seed", str(args.seed),
           "--builder", args.builder, "--deterministic", str(int(args.deterministic))]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        return {"device_requested": device, "error": r.stderr[-1500:]}
    res = json.loads(r.stdout.strip().splitlines()[-1])
    res["fallback_lines_in_stderr"] = [ln for ln in r.stderr.splitlines() if "fall back" in ln.lower() or "fallback" in ln.lower()][:20]
    res["cpu_fallbacks"] = sorted({*res.get("warnings", []), *res["fallback_lines_in_stderr"]} if any(
        "fall" in x.lower() for x in [*res.get("warnings", []), *res["fallback_lines_in_stderr"]]) else [])
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--steps", type=int, default=45)
    ap.add_argument("--seed", type=int, default=41)
    ap.add_argument("--builder", default="OrderedSwitchNet")
    ap.add_argument("--loss-tol", type=float, default=5e-3)
    ap.add_argument("--acc-tol", type=float, default=0.02)
    ap.add_argument("--deterministic", type=int, default=1, help="torch.use_deterministic_algorithms (retried off if the device refuses)")
    ap.add_argument("--require-mps", action="store_true", help="fail (exit 1) when MPS is unavailable; use on the Mac")
    ap.add_argument("--out")
    ap.add_argument("--child", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.child:
        print(json.dumps(child(a.child, a.steps, a.seed, a.builder, bool(a.deterministic))))
        return 0
    cpu, mps = run_device("cpu", a), run_device("mps", a)
    for r in (cpu, mps):
        r.pop("params", None)
    cmp: dict = {"loss_tol": a.loss_tol, "acc_tol": a.acc_tol, "match_within_tolerance": None}
    if mps.get("available") and "error" not in mps and "error" not in cpu:
        dl, da = abs(cpu["final_loss"] - mps["final_loss"]), abs(cpu["eval_accuracy"] - mps["eval_accuracy"])
        cmp.update(abs_final_loss_diff=dl, abs_accuracy_diff=da, param_checksum_diff=abs(cpu["param_checksum"] - mps["param_checksum"]),
                   match_within_tolerance=bool(dl <= a.loss_tol and da <= a.acc_tol), exact_equal=cpu["final_loss"] == mps["final_loss"])
    receipt = {"script": "mps_smoke.py", "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "config": {"steps": a.steps, "seed": a.seed, "builder": a.builder, "PYTORCH_ENABLE_MPS_FALLBACK": "1"},
               "python": sys.version.split()[0], "cpu": cpu, "mps": mps, "comparison": cmp,
               "ok": "error" not in cpu and "error" not in mps and cmp["match_within_tolerance"] is not False
               and (bool(mps.get("available")) or not a.require_mps)}
    text = json.dumps(receipt, indent=2)
    if a.out:
        Path(a.out).write_text(text)
    print(text)
    return 0 if receipt["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
