"""Generic, train-standardised linear PyTorch baseline. No card-specific branches."""

import argparse
import hashlib
import json
import time
from pathlib import Path

import numpy as np
import torch
from torch import nn

torch.set_num_threads(1)


class Model(nn.Module):
    def __init__(self, nin, nout):
        super().__init__()
        self.register_buffer("mean", torch.zeros(nin))
        self.register_buffer("scale", torch.ones(nin))
        self.register_buffer("target_mean", torch.tensor(0.0))
        self.register_buffer("target_scale", torch.tensor(1.0))
        self.linear = nn.Linear(nin, nout)

    def forward(self, x):
        return self.linear((x - self.mean) / self.scale)


def train(inputs, out, seed):
    started = time.perf_counter()
    inputs, out = Path(inputs), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    task = json.loads((inputs / "task.json").read_text())
    with np.load(inputs / "train.npz", allow_pickle=False) as data:
        x = torch.tensor(data["x"], dtype=torch.float32)
        y = torch.tensor(data["y"])
    torch.manual_seed(seed)
    torch.use_deterministic_algorithms(True)
    model = Model(x.shape[1], task["nclasses"])
    model.mean.copy_(x.mean(0))
    model.scale.copy_(x.std(0, correction=0).clamp_min(1e-6))
    classification = task["task_type"] == "classification"
    if not classification:
        y = y.float()
        model.target_mean.copy_(y.mean())
        model.target_scale.copy_(y.std(correction=0).clamp_min(1e-6))
        y = (y - model.target_mean) / model.target_scale
    optimizer = torch.optim.Adam(model.parameters(), lr=0.01)
    for _ in range(80):
        prediction = model(x)
        loss = (nn.functional.cross_entropy(prediction, y.long()) if classification else
                nn.functional.mse_loss(prediction[:, 0], y))
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
    torch.save(dict(config=dict(nin=x.shape[1], nout=task["nclasses"]),
                    task_type=task["task_type"], state_dict=model.state_dict()), out / "model.pt")
    infer(out / "model.pt", inputs / "validation.npz", out / "validation_predictions.npz")
    receipt = dict(seed=seed, steps=80, examples_seen=80 * len(y), device="cpu", threads=1,
                   elapsed_seconds=time.perf_counter() - started,
                   model_sha256=hashlib.sha256((out / "model.pt").read_bytes()).hexdigest())
    (out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


def infer(checkpoint, inputs, out):
    saved = torch.load(checkpoint, map_location="cpu", weights_only=True)
    model = Model(**saved["config"])
    model.load_state_dict(saved["state_dict"])
    model.eval()
    with np.load(inputs, allow_pickle=False) as data:
        x, ids = data["x"], data["ids"]
    with torch.no_grad():
        prediction = model(torch.tensor(x, dtype=torch.float32))
        predictions = (prediction.argmax(-1).numpy() if saved["task_type"] == "classification" else
                       (prediction[:, 0] * model.target_scale + model.target_mean).numpy())
    np.savez(out, ids=ids, predictions=predictions)


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    fit = commands.add_parser("train")
    fit.add_argument("--inputs", default=".")
    fit.add_argument("--out", required=True)
    fit.add_argument("--seed", type=int, default=11)
    pred = commands.add_parser("infer")
    pred.add_argument("--model", required=True)
    pred.add_argument("--inputs", required=True)
    pred.add_argument("--out", required=True)
    args = parser.parse_args()
    if args.command == "train":
        train(args.inputs, args.out, args.seed)
    else:
        infer(args.model, args.inputs, args.out)


if __name__ == "__main__":
    main()
