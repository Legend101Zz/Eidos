"""Pilot-2 completion and timing regressions; subprocess fixtures make no model calls."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from study.common import write_json
from study.launcher import outer_deadline
from study.run_arm import run_claude, run_stream

from . import arm4, worker
from .charters import charter
from .common import classify


def abstention(root):
    cost = dict.fromkeys(('elapsed_seconds', 'cpu_core_seconds', 'gpu_device_seconds', 'gpu_type',
                         'model_input_tokens', 'model_output_tokens', 'model_reasoning_tokens',
                         'model_calls', 'billed_usd'))
    cost.update(unknown_fields=list(cost), receipt_paths=[])
    answer = dict(schema_version='eidos-answer/0.2', task_id='D03', run_id='fixture',
                  status='abstained', claim_type='no_positive_claim', claim='Insufficient evidence.',
                  scope='Fixture only.', candidate=None, measurements=[], evidence=[],
                  alternative_explanations=[], abstention=dict(abstained=True, reason='Insufficient evidence.',
                  unresolved=['Confirmation'], next_discriminating_test='Fresh-seed held-out evaluation.'),
                  cost=cost, exposures=[], deviations=[], confidence=None)
    write_json(root / 'out/answer.json', answer)


class FinalOutcomes(unittest.TestCase):
    def coding_run(self, root, arm, events, exit_code=0, **limits):
        write_json(root / 'arm.json', dict(arm=arm, card='D03', run_index=1, block_id='fixture'))
        code = 'import sys\n' + '\n'.join(f'print({json.dumps(e)!r}, flush=True)' for e in events)
        code += f'\nsys.exit({exit_code})'
        return run_stream(root, [sys.executable, '-u', '-c', code], dict(os.environ), **limits)

    def test_recovered_errors_then_valid_abstention_are_ok_in_every_arm(self):
        error = {'type': 'error', 'error': 'Request output could not be parsed; retrying.'}
        final = {'type': 'result', 'is_error': False, 'result': 'Valid abstention written.'}
        receipts = [{'failed_request': {'error': 'Request output could not be parsed; retrying.'}}]
        shown = {'status': 'concluded', 'turns': [{'stop_reason': 'error'}, {'stop_reason': 'concluded'}]}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            abstention(root)
            valid = arm4.validate(root / 'out/answer.json')
            for arm in (1, 2, 3):
                with self.subTest(arm=arm):
                    rec = self.coding_run(root, arm, [error, {'type': 'result', 'is_error': True,
                                                           'errors': ['Earlier failed request']}, final])
                    self.assertEqual(rec['status'], 'ok')
                    self.assertIn(error['error'], (root / 'transcript.jsonl').read_text())
            self.assertEqual(classify(shown, receipts, ['shared'], answer_valid=valid), ('ok', None))

    def test_valid_final_result_survives_process_exit_error(self):
        with tempfile.TemporaryDirectory() as d:
            rec = self.coding_run(Path(d), 1, [{'type': 'result', 'is_error': False,
                                              'result': 'Answer written.'}], exit_code=1)
        self.assertEqual(rec['status'], 'ok')
        self.assertEqual(rec['exit_code'], 1)

    def test_failed_or_missing_final_result_still_crashes_in_every_arm(self):
        for arm in (1, 2, 3):
            for events in ([], [{'type': 'result', 'is_error': True, 'errors': ['Unrecovered error']}],
                           [{'type': 'result', 'is_error': False, 'result': 'Earlier result'},
                            {'type': 'result', 'is_error': True, 'errors': ['Final error']}]):
                with self.subTest(arm=arm, events=events), tempfile.TemporaryDirectory() as d:
                    self.assertEqual(self.coding_run(Path(d), arm, events)['status'], 'crash')
        for status in ('active', 'stopped', 'paused'):
            with self.subTest(status=status):
                self.assertEqual(classify({'status': status}, [], [], answer_valid=True)[0], 'crash')

    def test_malformed_concluded_answer_remains_invalid_output(self):
        self.assertEqual(classify({'status': 'concluded'}, [{'error': 'Recovered parse error'}], [],
                                  answer_valid=False)[0], 'invalid_output')

    def test_success_event_does_not_hide_invalid_coding_answer(self):
        final = {'type': 'result', 'is_error': False, 'result': 'Answer written.'}
        command = [sys.executable, '-u', '-c', f'print({json.dumps(final)!r})']
        check_output = subprocess.check_output

        def output(args, **kwargs):
            return 'fixture-key' if args[0] == 'security' else check_output(args, **kwargs)

        for arm in (1, 2):
            with self.subTest(arm=arm), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                write_json(root / 'arm.json', dict(arm=arm, card='D03', run_index=1, block_id='fixture'))
                write_json(root / 'out/answer.json', {})
                with (patch('study.run_arm.agent_env', return_value=dict(os.environ)),
                      patch('study.run_arm.sandbox_command', return_value=command),
                      patch('study.run_arm.subprocess.check_output', side_effect=output),
                      patch('study.toolkit.build_toolkit.build_toolkit')):
                    self.assertEqual(run_claude(root, arm)['status'], 'invalid_output')

    def test_incident_timeout_and_budget_still_take_precedence(self):
        final = {'type': 'result', 'is_error': False, 'result': 'Answer written.'}
        shown = {'status': 'concluded', 'turns': [{'stop_reason': 'error'}, {'stop_reason': 'concluded'}]}
        for error in ('HTTP 429', 'HTTP 503', 'quota exhausted'):
            for arm in (1, 2, 3):
                with self.subTest(arm=arm, error=error), tempfile.TemporaryDirectory() as d:
                    rec = self.coding_run(Path(d), arm, [{'type': 'error', 'error': error}, final])
                    self.assertEqual(rec['status'], 'incident')
            self.assertEqual(classify(shown, [{'error': error}], [])[0], 'incident')
        self.assertEqual(classify(shown, [{'error': 'Request timeout'}], [], timed_out=True)[0], 'timeout')
        budget = {**shown, 'turns': [*shown['turns'], {'stop_reason': 'budget-exhausted'}]}
        self.assertEqual(classify(budget, [], [])[0], 'token_budget')
        self.assertEqual(classify(shown, [], ['a', 'b'])[0], 'incident')
        # These incidental token counts must never look like a provider 5xx status.
        self.assertEqual(classify(shown, [{'error': 'Recovered parse error', 'output_tokens': 541}], [])[0], 'ok')
        for arm in (1, 2, 3):
            with self.subTest(arm=arm), tempfile.TemporaryDirectory() as d:
                rec = self.coding_run(Path(d), arm, [{**final, 'usage': {'output_tokens': 101}}], token_limit=100)
                self.assertEqual(rec['status'], 'token_budget')


class SetupAndInformation(unittest.TestCase):
    def test_setup_failure_still_reports_preparation_duration(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            write_json(root / 'arm.json', dict(arm=4, card='D03', run_index=1, block_id='fixture'))
            with (patch.object(arm4.lab, 'home', return_value=root / 'lab'),
                  patch.object(arm4.lab, 'build_record', return_value={'eidos_commit_sha': 'fixture'}),
                  patch.object(arm4, 'run', side_effect=RuntimeError('Fixture setup failure')),
                  patch.object(sys, 'argv', ['worker', '--arm', '4', '--run-dir', str(root)]),
                  redirect_stdout(io.StringIO())):
                self.assertEqual(worker.main(), 1)
            record = json.loads((root / 'run_record.json').read_text())
            self.assertEqual(record['status'], 'incident')
            self.assertFalse(record['setup_complete'])
            self.assertGreaterEqual(record['setup_wall_s'], 0)
            self.assertTrue(record['setup_started_at'])
            self.assertTrue(record['setup_ended_at'])

    def test_method_embedding_and_setup_receipt_at_real_adapter_boundary(self):
        clock = SimpleNamespace(now=100.0)
        imported = dict(charter_id='charter', artifacts={}, task={})
        shown = dict(status='concluded', turns=[], body={'conclusion': {'summary': 'Fixture result.'}})
        captured = {}

        def start():
            clock.now = 110.0

        def prepare(*args):
            clock.now = 400.0  # Five minutes of setup, longer than the 30-second run allowance.
            return 'project', imported

        class Client:
            def command(self, name, args, **kwargs):
                if name == 'start_investigation':
                    captured.update(args)
                    clock.now = 402.0
                    return {'result': {'investigation_id': 'inv'}}

            def query(self, *args, **kwargs):
                return shown

            def close(self):
                pass

        bridge = SimpleNamespace(pid=42, returncode=0, poll=lambda: 0)
        receipt = dict(base_url='http://127.0.0.1:1/v1', upstream='https://example.invalid/v1')
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'task').mkdir()
            (root / 'task/brief.md').write_text('Fixture brief')
            meta = dict(arm=4, card='D03', run_index=1, block_id='fixture', envelope=dict(wall_s=30))
            write_json(root / 'arm.json', meta)

            def export(*args, **kwargs):
                abstention(root)
                clock.now = 405.0

            with (patch.object(arm4, 'time', SimpleNamespace(monotonic=lambda: clock.now,
                                                           time=lambda: 1000 + clock.now)),
                  patch.object(arm4.lab, 'start', side_effect=start),
                  patch.object(arm4.lab, 'client', return_value=Client()),
                  patch.object(arm4.lab, 'guard_disk'),
                  patch.object(arm4.lab, 'home', return_value=root / 'lab'),
                  patch.object(arm4.lab, 'build_record', return_value={'eidos_commit_sha': 'fixture'}),
                  patch.object(arm4, 'prepare', side_effect=prepare),
                  patch.object(arm4, 'ledger', return_value=(shown, [], {'assessment': [], 'evidence_plan': []})),
                  patch.object(arm4, 'usage', return_value=({}, [], None, [])),
                  patch.object(arm4, 'select_job', return_value=('selected', 'fixture')),
                  patch.object(arm4, 'export_answer', side_effect=export),
                  patch('study.eidos_arms.provider_bridge.start', return_value=(bridge, receipt)),
                  patch.object(sys, 'argv', ['worker', '--arm', '4', '--run-dir', str(root)]),
                  redirect_stdout(io.StringIO())):
                self.assertEqual(worker.main(), 0)
            record = json.loads((root / 'run_record.json').read_text())
            self.assertEqual(record['setup_wall_s'], 300.0)
            self.assertTrue(record['setup_complete'])
            self.assertEqual(record['investigation_wall_s'], 5.0)
            self.assertEqual(json.loads((root / 'investigation.json').read_text())['deadline_unix'], 1430.0)
            embedded = json.loads(captured['question'].split('Packaged task files:\n', 1)[1])
            self.assertEqual(embedded['METHOD.md'], (arm4.ROOT / 'study/METHOD.md').read_text())
            self.assertEqual(embedded['brief.md'], 'Fixture brief')

    def test_launcher_does_not_charge_setup_or_change_other_arms(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertIsNone(outer_deadline(root, 4, 1860))
            for arm in (1, 2, 3):
                self.assertEqual(outer_deadline(root, arm, 1860), 1860)
            self.assertEqual(outer_deadline(root, 4, 1860, stub=True), 1860)
            write_json(root / 'setup.json', {'setup_wall_s': 300.5})
            self.assertEqual(outer_deadline(root, 4, 1860), 2160.5)

    def test_policy_is_prospective_and_in_each_pilot_2_charter(self):
        from eidos.domain.charter import validate_charter
        for card in ('D03', 'D04', 'D05', 'D06'):
            body = validate_charter(charter(card))
            self.assertIn('setup_wall_s', body['pilot_2_setup']['arm4_timing'])
        for card in ('D01', 'D02', 'D12'):
            self.assertNotIn('pilot_2_setup', charter(card))

    def test_baseline_note_matches_arm3_verbatim(self):
        from study.common import render_prompt
        note = ('For baseline reproduction, inputs.source is unnecessary: '
                'eidos.project.command already copies the supplied registered project.')
        for arm in (3, 4):
            self.assertIn(note, render_prompt(arm, 'D03'))


if __name__ == '__main__':
    unittest.main()
