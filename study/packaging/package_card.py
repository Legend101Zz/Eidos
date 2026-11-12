"""Controller-only allowlist packaging; public development sources remain read-only."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
SOURCE = Path(__file__).resolve().parents[2] / 'research/public/development'
TASKS = {'D03': 'digits_shift', 'D04': 'conditioning', 'D05': 'forecast', 'D06': 'order',
         'N01': 'breast_cancer', 'N02': 'diabetes', 'N03': 'friedman1', 'N04': 'classification'}

def package(task_id, out):
    sys.path.insert(0, str(SOURCE))
    from data import dataset
    out = Path(out)
    if out.exists() and any(out.iterdir()): raise ValueError('output must be empty')
    prospective = task_id.startswith('N')
    if prospective:
        from study.packaging.p3_data import card_spec, dataset as prospective_dataset
        card = card_spec(task_id)
        data, kind, classes = prospective_dataset(task_id)
    else:
        card = json.loads((SOURCE / 'tasks' / (task_id + '.json')).read_text())
        data, kind, classes = dataset(TASKS[task_id], seed=card['dataset_seed'])
    expected = 'classification' if task_id in ('D03', 'D06', 'N01', 'N04') else 'regression'
    if kind != expected: raise ValueError('card/dataset metric mismatch')
    out.mkdir(parents=True, exist_ok=True)
    for split in ('train', 'validation'):
        x, y, ids = data[split]
        np.savez_compressed(out / (split + '.npz'), x=x, y=y, ids=ids)
    shutil.copyfile(Path(__file__).with_name('baseline_project.py'), out / 'project.py')
    shutil.copyfile(SOURCE / 'requirements.lock', out / 'requirements.lock')
    shutil.copyfile(SOURCE / 'validate_answer.py', out / 'answer_schema.py')
    brief = (card['title'] + '. Work within the assigned research envelope. ' + card['card_scope'] + '. '
             'Use training and validation data for selection. Submit a reproducible recipe and scoped evidence; '
             'fresh initialization confirmation is required.\n\n'
             'Run: python project.py train --inputs . --out ../baseline --seed 11\n'
             'Input arrays: x float32 [N,D], y [N], ids int64 [N]. Output: ids int64 [N], predictions [N] '
             + ('int64 class IDs.' if kind == 'classification' else 'finite real values.') +
             ' No test feedback during search. No paid calls. Research budget must be assigned separately.\n')
    (out / 'brief.md').write_text(brief)
    task = dict(schema_version='eidos-task-bundle/0.1', task_id=task_id, project=TASKS[task_id],
                role='public_development', nclasses=classes, task_type=kind,
                inputs={k:dict(shape=list(data[k][0].shape), dtype='float32') for k in ('train','validation')},
                metric='accuracy' if kind == 'classification' else 'rmse',
                direction='maximize' if kind == 'classification' else 'minimize',
                denominator='fixed evaluator input IDs', allowed_operations=card['permitted_operations'],
                reference_visibility='excluded from this investigator bundle; controller only' if prospective else 'excluded from this investigator bundle; public elsewhere',
                qualification='see QUALIFICATION_P3.md' if prospective else 'local CPU qualification only; see QUALIFICATION_P2.md',
                resource_profile={'threads':1,'cpu_cores':1,'ram_gib_estimate':2,'disk_gib_environment_estimate':4,'reference_timeout_seconds':120},
                investigator_budget='UNDECIDED', answer_schema='eidos-answer/0.2',
                hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.iterdir())})
    assert (task['metric'], task['direction']) == (('accuracy','maximize') if kind == 'classification' else ('rmse','minimize'))
    (out / 'task.json').write_text(json.dumps(task, indent=2)+'\n')
    return task

if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--task',choices=TASKS,required=True);p.add_argument('--out',required=True)
    a=p.parse_args();package(a.task,a.out);print('Investigator bundle prepared; references and test material excluded.')
