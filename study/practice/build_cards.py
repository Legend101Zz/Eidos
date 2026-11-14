"""Write deterministic, neutral bundles outside the repository, with a separate controller tree."""
import argparse
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

import numpy as np

from study.practice.generators import generate, registry

ROOT = Path(__file__).resolve().parents[2]


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n')


def write_npz(path, **arrays):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in sorted(arrays.items()):
            buffer = io.BytesIO()
            np.lib.format.write_array(buffer, np.asarray(value), allow_pickle=False)
            info = zipfile.ZipInfo(name + '.npy', date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, buffer.getvalue())


def tree_digest(root):
    root = Path(root)
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode() + b'\0')
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def audit_bundles(root):
    root = Path(root)
    index = json.loads((root / 'index.json').read_text())['cards']
    counts = {}
    for item in index:
        card = item['card']
        if not re.fullmatch(r'P\d{4}', card):
            raise ValueError('non-neutral practice id')
        bundle = root / 'bundles' / card
        allowed = {'train.npz', 'validation.npz', 'task.json', 'project.py', 'requirements.lock'}
        if {p.name for p in bundle.iterdir()} != allowed:
            raise ValueError('bundle contains a non-allowlisted file')
        task_text = (bundle / 'task.json').read_text()
        if 'oracle_' in task_text or 'gen-' in task_text:
            raise ValueError('oracle or generator metadata leaked into bundle')
        with np.load(root / 'controller' / card / 'test_labels.npz', allow_pickle=False) as values:
            held_ids = set(values['ids'])
        visible = set()
        counts[card] = {}
        for split in ('train', 'validation'):
            with np.load(bundle / f'{split}.npz', allow_pickle=False) as values:
                if not {'x', 'y', 'ids'} <= set(values.files) or set(values.files) - {'x', 'y', 'ids', 'groups'}:
                    raise ValueError('bundle contains non-visible arrays')
                ids = set(values['ids'])
                if len(ids) != len(values['ids']) or ids & (held_ids | visible):
                    raise ValueError('held-out or duplicate IDs in visible bundle')
                visible.update(ids)
                counts[card][split] = len(ids)
        with np.load(root / 'controller' / card / 'test_inputs.npz', allow_pickle=False) as values:
            if set(values.files) != {'x', 'ids'} or set(values['ids']) != held_ids:
                raise ValueError('test inputs contain labels or mismatched IDs')
        if any(b'oracle_' in p.read_bytes() for p in bundle.iterdir()):
            raise ValueError('oracle fields in bundle')
    return dict(ok=True, bundles=len(index), visible_split_sizes=counts, data_digest=tree_digest(root))


def build(out, *, generator_ids=None, instances=4):
    out = Path(out).resolve()
    if out.is_relative_to(ROOT):
        raise ValueError('practice data must live outside the repository')
    if out.exists() and any(out.iterdir()):
        raise ValueError('output must be empty; use a new directory for a rerun')
    if instances < 1 or instances > 4:
        raise ValueError('instances must be between one and four')
    entries = registry()
    if generator_ids is not None:
        if set(generator_ids) - {g['id'] for g in entries}:
            raise ValueError('unknown generator id')
        entries = [g for g in entries if g['id'] in generator_ids]
    index = []
    for entry in entries:
        ordinal = next(i for i, g in enumerate(registry()) if g['id'] == entry['id'])
        for instance in range(instances):
            card = f'P{ordinal * 4 + instance + 1:04d}'
            data = generate(entry, instance)
            bundle, controller = out / 'bundles' / card, out / 'controller' / card
            for split in ('train', 'validation'):
                write_npz(bundle / f'{split}.npz', **data.splits[split])
            write_npz(controller / 'test_inputs.npz', **{k: data.splits['test'][k] for k in ('x', 'ids')})
            write_npz(controller / 'test_labels.npz', **{k: data.splits['test'][k] for k in ('y', 'ids')})
            write_json(controller / 'oracle.json', data.oracle)
            task = dict(schema_version='eidos-task-bundle/0.1', task_id=card, task_type=data.task_type,
                        nclasses=int(len(np.unique(data.splits['train']['y']))) if data.task_type == 'classification' else 1,
                        metric='accuracy' if data.task_type == 'classification' else 'rmse',
                        direction='maximize' if data.task_type == 'classification' else 'minimize',
                        resource_profile=dict(threads=1, reference_timeout_seconds=120))
            write_json(bundle / 'task.json', task)
            (bundle / 'project.py').write_text((Path(__file__).with_name('baseline.py')).read_text())
            (bundle / 'requirements.lock').write_text('numpy==2.5.4\nscikit-learn==1.9.1\njoblib==1.6.0\n')
            index.append(dict(card=card, generator=entry['id'], instance=instance, role=entry['role'],
                              bundle_digest=tree_digest(bundle), controller_digest=tree_digest(controller),
                              holdout_epoch=entry.get('holdout_epoch'), seed_stream=entry['seed_stream']))
    write_json(out / 'index.json', dict(schema='practice-index/2', holdout_epoch=max((e.get('holdout_epoch', 1) for e in entries), default=1), cards=index))
    digest = tree_digest(out)
    return dict(cards=len(index), data_digest=digest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.out), sort_keys=True))


if __name__ == '__main__':
    main()
