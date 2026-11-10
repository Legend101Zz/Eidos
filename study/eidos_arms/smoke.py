"""Prepare a tiny synthetic, non-study task and run a real MiniMax arm smoke.

python -m study.eidos_arms.smoke --arm 4 --run-dir SSD_DIR --wall-s 600
python -m study.eidos_arms.smoke --arm 3 --run-dir SSD_DIR --wall-s 300
No study cards, reference solutions or test labels are used.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from . import lab
from .common import ROOT, write_json

PROJECT = '''import argparse, json
from pathlib import Path
import numpy as np
import torch
torch.set_num_threads(1)

def model():
    return torch.nn.Linear(3, 2)

p = argparse.ArgumentParser()
p.add_argument('command', choices=['train','infer'])
p.add_argument('--inputs', required=True)
p.add_argument('--out', required=True)
p.add_argument('--model')
p.add_argument('--seed', type=int, default=11)
a = p.parse_args()
if a.command == 'train':
    torch.manual_seed(a.seed)
    data = np.load(Path(a.inputs)/'train.npz', allow_pickle=False)
    val = np.load(Path(a.inputs)/'validation.npz', allow_pickle=False)
    m = model()
    opt = torch.optim.SGD(m.parameters(), lr=0.15)
    x, y = torch.from_numpy(data['x']), torch.from_numpy(data['y'])
    for _ in range(40):
        opt.zero_grad()
        loss = torch.nn.functional.cross_entropy(m(x), y)
        loss.backward()
        opt.step()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    torch.save(m.state_dict(), out/'model.pt')
    with torch.no_grad():
        pred = m(torch.from_numpy(val['x'])).argmax(1).numpy()
    np.savez(out/'validation_predictions.npz', ids=val['ids'], predictions=pred)
    (out/'receipt.json').write_text(json.dumps({'accuracy':float((pred == val['y']).mean()), 'n':len(pred)}))
else:
    m = model()
    m.load_state_dict(torch.load(a.model, map_location='cpu', weights_only=True))
    data = np.load(a.inputs, allow_pickle=False)
    with torch.no_grad():
        pred = m(torch.from_numpy(data['x'])).argmax(1).numpy()
    np.savez(a.out, ids=data['ids'], predictions=pred)
'''

SMOKE_PROMPT = '''Work on the tiny synthetic classification task in task/. Read brief.md, task.json,
project.py and answer_schema.py. This is an operational smoke on synthetic data, not D01, D02 or D12.
Your tools: {{TOOLS_PARAGRAPH}}
Use CPU with one computational thread. Reproduce the supplied baseline with one completed training
job, inspect the receipt and validation predictions, and lock that training job. One successful
baseline is sufficient: do not spend the whole envelope searching. State the measured result and
limits honestly. Do not request operator input. The assigned wall ceiling is in arm.json.
For arm 4, use the investigator's decision schema and conclude; the controller writes the answer.
For arm 3, call at least list_components and run_component through MCP, poll get_run, download the
model and validation predictions through MCP artifact content paths, and write out/answer.json using
eidos-answer/0.2 from task/answer_schema.py. Use status inconclusive, claim_type no_positive_claim,
and report that predictions are visible-validation only. No test data or independent confirmation
is available. Keep all files under this run directory; never print credentials.
For this operational smoke, make the native list_components call once, then prefer mcp_http.py for
run_component with a JSON argument file so nested arrays and numeric types remain exact. Use the
registered task project unchanged; no source upload is needed for this baseline. One successful
baseline training job is enough. Its receipt already reports visible-validation accuracy; no extra
scoring, inference, fresh-seed training, source authoring or evidence-plan job is required here.
Every candidate/evidence/cost path in out/answer.json is relative to out/, so if you write
out/project.py the code_path is "project.py" (not "out/project.py"). Recompute every referenced
file's SHA-256, verify every path exists relative to out/, and run the structural validator before
concluding. reproduce_command is an argv array such as ["python","project.py","train","--inputs",
"../task","--out","reproduced","--seed","11"], not a list of shell-command strings.
'''


def prepare(run_dir, arm, wall):
    if run_dir.exists() and any(run_dir.iterdir()):
        raise ValueError('smoke directory must be fresh')
    task = run_dir / 'task'
    task.mkdir(parents=True)
    # Dataset generation runs with the research interpreter, entirely under the smoke directory.
    code = '''import numpy as np,sys
from pathlib import Path
rng=np.random.default_rng(8675309)
for name,n,start in [('train',160,0),('validation',60,1000)]:
    x=rng.normal(size=(n,3)).astype('float32')
    y=(x[:,0]+0.7*x[:,1]-0.3*x[:,2]>0).astype('int64')
    np.savez(Path(sys.argv[1])/(name+'.npz'),x=x,y=y,ids=np.arange(start,start+n,dtype='int64'))
'''
    subprocess.run([os.environ['EIDOS_RESEARCH_PYTHON'], '-c', code, str(task)],
                   env=lab.environment(), check=True)
    (task / 'project.py').write_text(PROJECT)
    versions = subprocess.check_output([os.environ['EIDOS_RESEARCH_PYTHON'], '-c',
        'from importlib.metadata import version; print("numpy=="+version("numpy")); print("torch=="+version("torch"))'],
        env=lab.environment(), text=True)
    (task / 'requirements.lock').write_text(versions)
    shutil.copyfile(ROOT / 'research/public/development/validate_answer.py', task / 'answer_schema.py')
    (task / 'brief.md').write_text('Reproduce the tiny linear classifier baseline. This is an operational smoke; '
        'no model-improvement claim is required. Run python project.py train --inputs . --out ../baseline --seed 11. '
        'Inputs are train.npz and validation.npz; output model.pt and validation_predictions.npz.\n')
    manifest = {'schema_version': 'eidos-task-bundle/0.1', 'task_id': 'SYNTHETIC_LINEAR_SMOKE',
        'nclasses': 2, 'metric': 'accuracy', 'answer_schema': 'eidos-answer/0.2',
        'resource_profile': {'threads': 1, 'cpu_cores': 1},
        'hashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in task.iterdir()}}
    write_json(task / 'task.json', manifest)
    write_json(run_dir / 'arm.json', {'arm': arm, 'card': manifest['task_id'], 'run_index': 0,
        'block_id': 'non-study-smoke', 'model': 'MiniMax-M3.1-Flash',
        'envelope': {'wall_s': wall, 'tokens': 8_000_000, 'threads': 1}, 'charter_sha': None})
    (run_dir / 'smoke_prompt.md').write_text(SMOKE_PROMPT)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--arm', type=int, choices=(3, 4), required=True)
    p.add_argument('--run-dir', type=Path, required=True)
    p.add_argument('--wall-s', type=int, required=True)
    args = p.parse_args()
    lab.guard_disk()
    run_dir = args.run_dir.resolve()
    prepare(run_dir, args.arm, args.wall_s)
    env = lab.environment()
    env['EIDOS_STUDY_SMOKE_PROMPT'] = str(run_dir / 'smoke_prompt.md')
    return subprocess.run([lab.python(), '-m', 'study.eidos_arms.worker', '--arm', str(args.arm),
                          '--run-dir', str(run_dir)], cwd=ROOT, env=env, stdin=subprocess.DEVNULL).returncode


if __name__ == '__main__':
    raise SystemExit(main())
