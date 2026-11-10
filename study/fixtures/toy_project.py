"""Tiny regression fixture used only for the single live runner smoke."""

import argparse
from pathlib import Path

import numpy as np
import torch

torch.set_num_threads(1)


def train(inputs, out, seed):
    torch.manual_seed(seed)
    out = Path(out)
    out.mkdir(exist_ok=True, parents=True)
    with np.load(Path(inputs) / "train.npz", allow_pickle=False) as data:
        x, y = torch.tensor(data["x"]), torch.tensor(data["y"])
    model = torch.nn.Linear(1, 1)
    optimizer = torch.optim.SGD(model.parameters(), lr=0.1)
    for _ in range(100):
        loss = ((model(x)[:, 0] - y) ** 2).mean()
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
    torch.save(model.state_dict(), out / "model.pt")
    infer(out / "model.pt", Path(inputs) / "validation.npz", out / "validation_predictions.npz")


def infer(model_path, inputs, out):
    model = torch.nn.Linear(1, 1)
    model.load_state_dict(torch.load(model_path, map_location="cpu", weights_only=True))
    with np.load(inputs, allow_pickle=False) as data, torch.no_grad():
        np.savez(out, ids=data["ids"], predictions=model(torch.tensor(data["x"]))[:, 0].numpy())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    fitting = commands.add_parser("train")
    fitting.add_argument("--inputs", required=True)
    fitting.add_argument("--out", required=True)
    fitting.add_argument("--seed", type=int, default=11)
    inference = commands.add_parser("infer")
    inference.add_argument("--model", required=True)
    inference.add_argument("--inputs", required=True)
    inference.add_argument("--out", required=True)
    args = parser.parse_args()
    if args.command == "train":
        train(args.inputs, args.out, args.seed)
    else:
        infer(args.model, args.inputs, args.out)
