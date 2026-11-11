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
        self.net = nn.Linear(1 if kind == 'pool' else nin, nout)

    def forward(self, x):
        x = (x - self.mean) / self.scale
        if self.kind == 'constant':
            return torch.zeros((len(x), 2))
        if self.kind == 'last': return x[:, -1:]
        if self.kind == 'pool': x = x.mean(-1, keepdim=True)
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
    kind = {'order': 'pool', 'forecast': 'last'}.get(project, 'linear')
    steps = {'digits_shift':100, 'conditioning':160, 'forecast':0, 'order':60}[project]
    model = Model(nin, nout, kind, mean, scale)
    opt = torch.optim.Adam(model.parameters(), lr=.02)
    for _ in range(steps):
        prediction = model(x)
        loss = nn.functional.cross_entropy(prediction, y) if task['task_type'] == 'classification' else nn.functional.mse_loss(prediction[:, 0], y)
        opt.zero_grad(); loss.backward(); opt.step()
    config = dict(nin=nin, nout=nout, kind=kind)
    config['task_type'] = task['task_type']
    torch.save({'config':config, 'state_dict':model.state_dict()}, out/'model.pt')
    infer(out/'model.pt', root/'validation.npz', out/'validation_predictions.npz')
    receipt = dict(seed=seed, steps=steps, examples_seen=steps*len(y), device='cpu', threads=1,
                   elapsed_seconds=time.perf_counter()-started,
                   model_sha256=hashlib.sha256((out/'model.pt').read_bytes()).hexdigest())
    (out/'receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')

def infer(checkpoint, inputs, output):
    # This is a local candidate process, never the trusted label-owning scorer.
    saved = torch.load(checkpoint, map_location='cpu', weights_only=True)
    config = dict(saved['config']); task_type = config.pop('task_type')
    model = Model(**config); model.load_state_dict(saved['state_dict']); model.eval()
    with np.load(inputs, allow_pickle=False) as data:
        x, ids = data['x'], data['ids']
    with torch.no_grad():
        prediction = model(torch.tensor(x))
        predictions = prediction.argmax(-1).numpy() if task_type == 'classification' else prediction[:, 0].numpy()
    np.savez(output, ids=ids, predictions=predictions)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    fit = commands.add_parser('train'); fit.add_argument('--inputs', default='.'); fit.add_argument('--out', required=True); fit.add_argument('--seed', type=int, default=11)
    pred = commands.add_parser('infer'); pred.add_argument('--model', required=True); pred.add_argument('--inputs', required=True); pred.add_argument('--out', required=True)
    args = parser.parse_args()
    if args.command == 'train': train(args.inputs, args.out, args.seed)
    else: infer(args.model, args.inputs, args.out)
