"""Export an explicit investigator allowlist for three qualified smoke cases.
Run as the research owner; give only the created bundle to the investigator.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import numpy as np
from data import dataset

TASKS = {'D01':'iris', 'D02':'digits', 'D12':'null'}

def package(task_id, out):
    source = Path(__file__).resolve().parent
    out = Path(out)
    if out.exists() and any(out.iterdir()): raise ValueError('output must be empty')
    out.mkdir(parents=True, exist_ok=True)
    project = TASKS[task_id]
    data, kind, nclasses = dataset(project)
    for split in ['train', 'validation']:
        x, y, ids = data[split]
        np.savez_compressed(out/f'{split}.npz', x=x, y=y, ids=ids)
    shutil.copyfile(source/'agent_project.py', out/'project.py')
    shutil.copyfile(source/'requirements.lock', out/'requirements.lock')
    brief = {
        'D01':'Import this ordinary PyTorch model, train it, and demonstrate behavior-preserving export/reload. This is an operational exercise; no model-improvement claim is required.',
        'D02':'Improve this functioning digit classifier within the assigned research envelope. Use training and validation data for selection. Submit a reproducible recipe and scoped evidence; fresh initialization confirmation is required for a recipe or architecture claim.',
        'D12':'Investigate whether a reproducible classification improvement is supported within the assigned envelope. Report evidence and uncertainty; an unsuccessful search does not prove impossibility.'
    }[task_id]
    (out/'brief.md').write_text(brief+'\n\nRun: python project.py train --inputs . --out ../baseline --seed 11\nInput arrays: x float32 [N,D], y int64 [N], ids int64 [N]. Output predictions: ids int64 [N], predictions int64 [N]. No test feedback during search. No paid calls. Research budget must be assigned separately; only local reference execution is qualified.\n')
    task = dict(schema_version='eidos-task-bundle/0.1', task_id=task_id, project=project,
                role='public_development', nclasses=nclasses, task_type=kind,
                inputs={k:{'shape':list(data[k][0].shape), 'dtype':'float32'} for k in ['train','validation']},
                metric='accuracy', direction='maximize', denominator='fixed evaluator input IDs',
                allowed_operations=['edit model/trainer', 'train-only preprocessing/augmentation', 'create instruments'],
                reference_visibility='excluded from this investigator bundle; public elsewhere',
                qualification='baseline/export verified on macOS arm64 CPU; hosted Linux and Eidos integration unqualified',
                resource_profile={'threads':1,'cpu_cores':1,'ram_gib_estimate':2,'disk_gib_environment_estimate':4,'reference_timeout_seconds':120},
                investigator_budget='UNDECIDED', answer_schema='eidos-answer/0.2',
                hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.iterdir())})
    (out/'task.json').write_text(json.dumps(task,indent=2)+'\n')
    return task

if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--task', choices=TASKS, required=True);parser.add_argument('--out', required=True);args=parser.parse_args()
    package(args.task,args.out);print('Investigator bundle prepared; references and test material excluded.')
