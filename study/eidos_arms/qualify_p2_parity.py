"""Model-free pilot-2 environment and real protocol-control qualification."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

from . import lab
from .common import ROOT, prepare, utc, write_json


def qualify(out):
    if 'pilot-1' in lab.home().parts or 'pilot-1' in lab.python():
        raise ValueError('Select a separate pilot-2 lab home and installed interpreter explicitly')
    if out.exists() and any(out.iterdir()):
        raise ValueError('qualification output must be empty')
    out.mkdir(parents=True, exist_ok=True)
    lab.start()
    c = lab.client()
    summaries = []
    try:
        from eidos.cli.main import wait_job
        for card in ('D03', 'D06'):
            lab.guard_disk()
            root = out / card
            root.mkdir()
            subprocess.run([os.environ['EIDOS_RESEARCH_PYTHON'], '-m', 'study.packaging.package_card',
                            '--task', card, '--out', str(root / 'task')], cwd=ROOT, check=True)
            write_json(root / 'arm.json', {'arm': 4, 'card': card})
            begin = time.monotonic()
            project, imported = prepare(c, root, 4)
            profile = c.query('get_profile', {}, project=project)['body']
            environments = {}
            for name, ref in profile['environments'].items():
                env = c.query('get_record', {'kind': 'environment', **ref}, project=project)['body']
                assert env['python_executable'] == os.environ['EIDOS_RESEARCH_PYTHON'], env['python_executable']
                assert Path(env['project_root']) == root / 'task', env['project_root']
                environments[name] = env
            assert environments['default']['installed'] == environments['lab']['installed']
            assert environments['default']['lock_digest'] == environments['lab']['lock_digest']
            jobs = {}
            for name in environments:
                # This command uses no authored source, imports all scientific dependencies,
                # then reproduces the supplied baseline via the ordinary registered project.
                code = ('import numpy,torch,scipy,sklearn,joblib,runpy,sys; '
                        'sys.argv=["project.py", "train", "--inputs", sys.argv[1], '
                        '"--out", sys.argv[2], "--seed", "11"]; '
                        'runpy.run_path("project.py", run_name="__main__")')
                r = c.command('run_component', {'component_ref': 'eidos.project.command@1.0.7',
                              'environment': name, 'inputs': imported['artifacts'], 'purpose': 'reproduction',
                              'config': {'argv': ['python', '-c', code, '{work}', '{out}'],
                                         'seed': 11, 'timeout_seconds': 120}}, project=project)['result']
                wait_job(c, project, r['job_id'])
                job = c.query('get_job', {'job_id': r['job_id'], 'include_lock': True}, project=project)
                write_json(root / f'baseline-{name}.json', job)
                assert job['status'] == 'succeeded', f'{card}/{name}: {job.get("terminal")}'
                assert 'source' not in job['lock']['inputs']
                assert any(o.get('role') == 'model-export' for o in job['outputs'])
                assert any(o['slot'] == 'validation_predictions' for o in job['outputs'])
                jobs[name] = job['id']
            qualifications = json.loads((root / 'protocol_qualification.json').read_text())['receipts']
            assert len(qualifications) == 2
            assert all(r['body']['status'] == 'passed' for r in qualifications)
            events = c.query('list_events', {'limit': 500}, project=project)
            assert not any(e['type'] == 'model.usage' for e in events['events'])
            assert not c.query('list_records', {'kind': 'investigation'}, project=project)
            summary = {'card': card, 'project': project, 'environments': environments,
                       'baseline_jobs': jobs, 'qualification_receipts': qualifications,
                       'wall_s': round(time.monotonic() - begin, 3), 'model_calls': 0}
            summaries.append(summary)
            print(f'{card}: default/lab research interpreters and package locks match; '
                  'scientific imports + no-source baseline PASS in both; '
                  'assessor/template qualifications passed; model_calls=0', flush=True)
        result = {'checked_at': utc(), 'build': lab.build_record(), 'cards': summaries}
        write_json(out / 'summary.json', result)
        return result
    finally:
        c.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    qualify(args.out.resolve())
