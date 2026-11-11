"""Prepare the built-in assessor and planner through real, recorded control gates.

Uses the existing journey's planning-template qualifier (an authored infrastructure
fixture, not independent scientific validation). No task data enters these controls.
"""
from __future__ import annotations

import copy
import json
import sys
import time

from . import lab
from .common import ROOT, write_json

ASSESSOR = 'eidos.assessors.paired-ci@1.0.0'
TEMPLATE = 'eidos.templates.improves-under-budget@1.1.0'
RUNNER = 'eidos.qualification-runner@1.0.0'


def prepare(c, project, run_dir):
    from eidos.cli.main import builtin_component_dirs, install_component
    from eidos.runtime.env_probe import probe_environment, registration_key
    payload = {'name': 'lab', 'python_executable': lab.python(),
               'project_root': str(run_dir / 'task'), 'python_version': sys.version.split()[0],
               'notes': 'Installed lab interpreter; only the packaged task is the project root',
               **probe_environment(lab.python(), project_root=run_dir / 'task')}
    c.command('register_environment', payload, project=project, key=registration_key('pilot-lab-env:' + project, payload))
    dirs = {p.name: p for p in builtin_component_dirs(lab_env_only=True)}
    for name in ('eidos.assessors.paired-ci', 'eidos.templates.improves-under-budget', 'eidos.qualification-runner'):
        result = install_component(c, project, dirs[name], 'lab', True)
        if 'component_ref' not in result or result.get('validation') == 'failed':
            raise RuntimeError(f'protocol installation failed: {result}')
    result = install_component(c, project, ROOT / 'tests/journeys/support/template-qualifier', 'lab', True)
    if result.get('validation') == 'failed' or 'component_ref' not in result:
        raise RuntimeError(f'template qualifier installation failed: {result}')
    assessor_suite = json.loads((dirs['eidos.assessors.paired-ci'] / 'controls/suite.json').read_text())
    # These requests only test planning/refusal, and do not run the named training recipes.
    base = {'claim': {'kind': 'improves', 'statement': 'Authored planning control'},
            'success_rule_id': 'practical-accuracy-gain', 'assessor': {'ref': ASSESSOR},
            'seeds': [11, 23, 37], 'common_train': {'steps': 1}, 'common_evaluate': {},
            'evaluation': {'split': 'validation'}, 'budget': {'steps': 1},
            'required_isolation': {'profile': 'dev:same-user', 'controls': []},
            'baseline': {'description': 'authored null'}, 'candidate': {'description': 'identical'}}
    planted = copy.deepcopy(base)
    planted['candidate']['post'] = [{'component': 'eidos.pytorch.weight-edit',
                                      'config': {'edits': [{'entry': 'weight', 'op': 'set', 'value': 0}]}}]
    template_suite = {'target': {'kind': 'template', 'ref': TEMPLATE}, 'scope': ['improves'],
        'origin': {'authored_by': 'pilot integration author controls; not independent', 'committed_before_run': True},
        'controls': [
            {'id': 'planted-distinct', 'type': 'planted', 'deterministic': True, 'repetitions': 1,
             'expected_outcome': 'distinct-arms-planned', 'input': {'inputs': {'request': planted}}},
            {'id': 'null-identical', 'type': 'null', 'deterministic': True, 'repetitions': 1,
             'expected_outcome': 'identical-arms', 'input': {'inputs': {'request': base}}},
            {'id': 'null-threshold-override', 'type': 'null', 'deterministic': True, 'repetitions': 1,
             'expected_outcome': 'refused', 'expect_refusal': True,
             'input': {'inputs': {'request': {**planted, 'min_effect': 0}}}}]}
    receipts = []
    for name, ref, runner, suite in [('assessor', ASSESSOR, RUNNER, assessor_suite),
                                    ('template', TEMPLATE, result['component_ref'], template_suite)]:
        sid = c.command('register_control_suite', {'suite_id': 'suite:pilot-' + name, 'suite': suite},
                        project=project)['result']['suite_id']
        q = c.command('qualify_protocol', {'suite_id': sid, 'release_ref': ref, 'runner_ref': runner,
                                          'wall_seconds': 120}, project=project)['result']
        until = time.monotonic() + 150
        while True:
            records = c.query('list_records', {'kind': 'qualification', 'parent_id': ref,
                                               'summary': False, 'limit': 10}, project=project)
            if records:
                receipt = records[-1]
                if receipt['body']['status'] != 'passed':
                    raise RuntimeError(f'{name} qualification failed: {receipt}')
                receipts.append(receipt)
                break
            job = c.query('get_job', {'job_id': q['job_id']}, project=project)
            if job['status'] in ('failed', 'cancelled') or time.monotonic() >= until:
                raise RuntimeError(f'{name} qualification did not complete: {job["status"]}')
            lab.guard_disk()
            time.sleep(.5)
        c.command('promote_protocol', {'release_ref': ref, 'scope': ['improves'],
                                      'note': 'Executed authored controls, not independent qualification'}, project=project)
    write_json(run_dir / 'protocol_qualification.json', {'project': project, 'receipts': receipts,
                                                       'independent_control_suite': False})
