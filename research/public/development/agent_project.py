"""Ordinary baseline project for the D01/D02/D12 investigator bundles.
Only consumes supplied train/validation arrays; no development references/labels.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time
import numpy as np
import torch
from torch import nn

torch.set_num_threads(1)

class Model(nn.Module):
    def __init__(self, nin, nout, kind, mean=None, scale=None):
        super().__init__()
        self.kind = kind
        self.register_buffer('mean', torch.zeros(nin) if mean is None else mean)
        self.register_buffer('scale', torch.ones(nin) if scale is None else scale)
        self.net = nn.Linear(nin, nout)

    def forward(self, x):
        x = (x - self.mean) / self.scale
        if self.kind == 'constant':
            return torch.zeros((len(x), 2))
        return self.net(x)

def train(root, out, seed):
    started = time.perf_counter()
    root, out = Path(root), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    task = json.loads((root/'task.json').read_text())
    with np.load(root/'train.npz', allow_pickle=False) as data:
        x = torch.tensor(data['x']); y = torch.tensor(data['y'])
    torch.manual_seed(seed)
    project = task['project']
    nin, nout = x.shape[1], task['nclasses']
    mean, scale = torch.zeros(nin), torch.ones(nin)
    if project == 'iris':
        mean, scale = x.mean(0), x.std(0).clamp_min(.001)
    kind = 'constant' if project == 'null' else 'linear'
    steps = {'iris':100, 'digits':8, 'null':0}[project]
    model = Model(nin, nout, kind, mean, scale)
    opt = torch.optim.Adam(model.parameters(), lr=.02)
    for _ in range(steps):
        loss = nn.functional.cross_entropy(model(x), y)
        opt.zero_grad(); loss.backward(); opt.step()
    config = dict(nin=nin, nout=nout, kind=kind)
    torch.save({'config':config, 'state_dict':model.state_dict()}, out/'model.pt')
    infer(out/'model.pt', root/'validation.npz', out/'validation_predictions.npz')
    receipt = dict(seed=seed, steps=steps, examples_seen=steps*len(y), device='cpu', threads=1,
                   elapsed_seconds=time.perf_counter()-started,
                   model_sha256=hashlib.sha256((out/'model.pt').read_bytes()).hexdigest())
    (out/'receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')

def infer(checkpoint, inputs, output):
    # This is a local candidate process, never the trusted label-owning scorer.
    saved = torch.load(checkpoint, map_location='cpu', weights_only=True)
    model = Model(**saved['config']); model.load_state_dict(saved['state_dict']); model.eval()
    with np.load(inputs, allow_pickle=False) as data:
        x, ids = data['x'], data['ids']
    with torch.no_grad():
        predictions = model(torch.tensor(x)).argmax(-1).numpy()
    np.savez(output, ids=ids, predictions=predictions)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    fit = commands.add_parser('train'); fit.add_argument('--inputs', default='.'); fit.add_argument('--out', required=True); fit.add_argument('--seed', type=int, default=11)
    pred = commands.add_parser('infer'); pred.add_argument('--model', required=True); pred.add_argument('--inputs', required=True); pred.add_argument('--out', required=True)
    args = parser.parse_args()
    if args.command == 'train': train(args.inputs, args.out, args.seed)
    else: infer(args.model, args.inputs, args.out)
