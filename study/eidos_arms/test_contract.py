"""Focused accounting, selection and incident regression checks; no model calls."""
import json
import socket
import tempfile
import time
import unittest
from pathlib import Path

from .arm4 import select_job
from .common import classify, usage


class Receipts(unittest.TestCase):
    def test_usage_counts_failures_once_and_separates_cached_input(self):
        events = [{'type': 'model.usage', 'body': {'usage': {'input_tokens': 100,
                   'output_tokens': 20, 'served_models': ['MiniMax-M3.1-Flash'], 'calls': 1}}},
                  {'type': 'decision.accepted', 'body': {'usage': {'input_tokens': 100}}},
                  {'type': 'model.usage', 'body': {'usage': {'input_tokens': 7, 'output_tokens': 3,
                   'uncertain': True, 'failed_request': {'status_code': 503}}}}]
        response = {'type': 'model_response', 'data': {'response': [{'kind': 'response',
            'provider_response_id': 'r1', 'usage': {'cache_read_tokens': 30, 'cache_write_tokens': 5}}]}}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'transcript.jsonl'
            path.write_text(json.dumps(response) + '\n' + json.dumps(response) + '\n')
            tokens, models, billed, receipts = usage(events, path)
        self.assertEqual(tokens, {'input': 72, 'cache_read': 30, 'cache_write': 5, 'output': 23, 'total': 130})
        self.assertEqual(models, ['MiniMax-M3.1-Flash'])
        self.assertIsNone(billed)
        self.assertEqual(len(receipts), 2)

    def test_incidents_take_precedence_over_timeout(self):
        self.assertEqual(classify({}, [{'failed_request': {'status_code': 529}}], [], True)[0], 'incident')
        self.assertEqual(classify({}, [], ['a', 'b'])[0], 'incident')
        self.assertEqual(classify({}, [], [], True)[0], 'timeout')
        self.assertEqual(classify({'needs_input': {'reason': 'budget-exhausted'}}, [], [])[0], 'token_budget')
        self.assertEqual(classify({'needs_input': {'reason': 'invalid proposals'}}, [], [])[0], 'crash')
        self.assertEqual(classify({}, [], [], answer_valid=False)[0], 'invalid_output')


class Selection(unittest.TestCase):
    def setUp(self):
        self.jobs = {}
        for jid, source in [('baseline', {}), ('search', {'source': {'artifact_id': 'source1'}})]:
            self.jobs[jid] = {'id': jid, 'created_at': jid, 'lock': {'inputs': source},
                'outputs': [{'role': 'model-export'}, {'slot': 'validation_predictions'}]}
        self.shown = {'jobs': [{'id': j, 'status': 'succeeded'} for j in self.jobs]}
        self.records = {'note': [], 'assessment': [], 'evidence_plan': []}
        outer = self
        class Client:
            def query(self, name, params, project):
                return outer.jobs[params['job_id']]
        self.client = Client()

    def test_unlocked_search_never_displaces_baseline(self):
        jid, _ = select_job(self.client, 'p', self.shown, self.records)
        self.assertEqual(jid, 'baseline')

    def test_explicit_lock_selects_successful_candidate(self):
        self.records['note'] = [{'id': 'lock1', 'body': {'kind': 'candidate_lock', 'text': '{"job_id":"search"}'}}]
        self.assertEqual(select_job(self.client, 'p', self.shown, self.records)[0], 'search')
        self.records['note'][0]['body']['text'] = '{"job_id":"missing"}'
        self.assertEqual(select_job(self.client, 'p', self.shown, self.records)[0], 'baseline')

    def test_confirmation_uses_real_eidos_outcome_and_health(self):
        self.records['assessment'] = [{'id': 'assessment1', 'body': {'plan_id': 'plan1',
            'outcome': 'protocol-passed', 'conformance': 'conformant', 'health': 'intact'}}]
        self.records['evidence_plan'] = [{'id': 'plan1', 'body': {
            'arms': [{'id': 'candidate-arm', 'role': 'candidate'}],
            'launch': {'jobs': {'candidate-arm': ['search']}}}}]
        self.assertEqual(select_job(self.client, 'p', self.shown, self.records)[0], 'search')
        self.records['assessment'][0]['body']['health'] = 'affected'
        self.assertEqual(select_job(self.client, 'p', self.shown, self.records)[0], 'baseline')


class ClaudeStreaming(unittest.TestCase):
    def test_partial_usage_survives_timeout_and_does_not_double_count_final(self):
        from .arm3 import lane_a
        from .stream_usage import extend_tracker
        try:
            _, _, module = lane_a()
        except ModuleNotFoundError:
            self.skipTest('lane A runner is available after merge, or via EIDOS_STUDY_LANE_A_ROOT')
        tracker = extend_tracker(module.UsageTracker)()
        tracker.observe({'type': 'stream_event', 'event': {'type': 'message_start',
            'message': {'id': 'r1', 'model': 'MiniMax-M3.1-Flash', 'usage': {'input_tokens': 0, 'output_tokens': 0}}}})
        tracker.observe({'type': 'stream_event', 'event': {'type': 'message_delta',
            'usage': {'input_tokens': 9, 'cache_read_input_tokens': 197, 'output_tokens': 10}}})
        self.assertEqual(tracker.tokens['total'], 216)
        tracker.observe({'type': 'assistant', 'message': {'id': 'r1', 'model': 'MiniMax-M3.1-Flash',
            'usage': {'input_tokens': 0, 'output_tokens': 0}}})
        tracker.observe({'type': 'result', 'usage': {'input_tokens': 9,
            'cache_read_input_tokens': 197, 'output_tokens': 10}, 'result': 'OK'})
        self.assertEqual(tracker.tokens['total'], 216)
        self.assertEqual(tracker.claim, 'OK')


class ProviderWall(unittest.TestCase):
    def test_deadline_exits_and_closes_a_blocked_request(self):
        from .provider_bridge import start
        with tempfile.TemporaryDirectory() as d:
            process, state = start(Path(d), 'https://example.invalid/v1', time.time() + 1.5, 300)
            try:
                from urllib.parse import urlparse
                port = urlparse(state['base_url']).port
                with socket.create_connection(('127.0.0.1', port), timeout=4) as conn:
                    # Leave the request body incomplete: no upstream request can occur.
                    conn.sendall(b'POST /v1/chat/completions HTTP/1.0\r\nAuthorization: Bearer fixture\r\n'
                                 b'Content-Length: 999\r\n\r\n')
                    self.assertEqual(conn.recv(1), b'')
                self.assertEqual(process.wait(timeout=3), 124)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


class ArtifactReferences(unittest.TestCase):
    def test_paths_and_digests_are_checked_relative_to_answer_directory(self):
        from .arm4 import validate
        from .common import digest
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'out'
            out.mkdir()
            candidate = {'parent_checkpoint': None, 'restore_class': 'fresh_initialization',
                         'reproduce_command': ['python', 'project.py', 'train']}
            for field, filename in [('code', 'project.py'), ('model', 'model.pt'), ('predictions', 'validation.npz')]:
                (out / filename).write_bytes(b'fixture')
                candidate[field + '_path'] = filename
                candidate[field + '_sha256'] = digest(out / filename)
            (out / 'requirements.lock').write_text('fixture==1\n')
            candidate.update(environment_lock='requirements.lock',
                             environment_lock_sha256=digest(out / 'requirements.lock'))
            cost = {k: None for k in ('elapsed_seconds', 'cpu_core_seconds', 'gpu_device_seconds', 'gpu_type',
                'model_input_tokens', 'model_output_tokens', 'model_reasoning_tokens', 'model_calls', 'billed_usd')}
            cost.update(unknown_fields=list(cost), receipt_paths=[])
            answer = {'schema_version': 'eidos-answer/0.2', 'task_id': 'fixture', 'run_id': 'fixture',
                'status': 'inconclusive', 'claim_type': 'no_positive_claim', 'claim': 'fixture', 'scope': 'fixture',
                'candidate': candidate, 'measurements': [], 'evidence': [], 'alternative_explanations': [],
                'abstention': {'abstained': False, 'reason': None, 'unresolved': [], 'next_discriminating_test': None},
                'cost': cost, 'exposures': [], 'deviations': [], 'confidence': None}
            path = out / 'answer.json'
            path.write_text(json.dumps(answer))
            self.assertTrue(validate(path))
            candidate['code_path'] = 'out/project.py'
            path.write_text(json.dumps(answer))
            with self.assertRaisesRegex(ValueError, 'missing or escapes'):
                validate(path)
            candidate['code_path'] = 'project.py'
            candidate['code_sha256'] = '0' * 64
            path.write_text(json.dumps(answer))
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                validate(path)


class ScientificAnswer(unittest.TestCase):
    def test_model_scientific_fields_survive_receipt_and_artifact_conversion(self):
        from .arm4 import export_answer, validate
        job = {'id': 'job_train', 'lock': {'inputs': {}, 'environment': {'installed': {'torch': '2.15.0'}},
               'config': {'argv': ['python', 'project.py', 'train', '--steps', '17'], 'seed': 41}},
               'outputs': [{'slot': 'source_snapshot', 'artifact_id': 'source'},
                           {'slot': 'model', 'role': 'model-export', 'artifact_id': 'model'},
                           {'slot': 'validation_predictions', 'artifact_id': 'pred'}]}

        class Client:
            def query(self, name, params, project):
                return job if params['job_id'] == 'job_train' else {'id': 'job_score', 'status': 'succeeded'}

            def download(self, project, aid, target):
                if aid == 'source':
                    Path(target).write_text(json.dumps({'files': {'project.py': 'print("fixture")\n'}}))
                else:
                    Path(target).write_bytes(b'fixture')

        metadata = {'status': 'improvement_claimed', 'claim_type': 'recipe_improvement',
            'claim': 'Investigator authored claim, kept verbatim.', 'scope': 'Visible-validation scope only',
            'measurements': [{'metric': 'accuracy', 'direction': 'higher_is_better', 'split': 'validation',
                'denominator': 10, 'units': 'fraction', 'baseline': .5, 'candidate': .7,
                'uncertainty_method': 'Investigator reports no independent confirmation', 'evidence_receipt': 'job_score'}],
            'alternative_explanations': ['selection'], 'confidence': .6, 'confidence_outcome': 'declared scoped effect'}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'task').mkdir()
            (root / 'task/requirements.lock').write_text('torch==2.7.0\n')
            export_answer(Client(), 'p', 'inv', root, {'card': 'synthetic'}, 'job_train', 'lock',
                          'verbatim conclusion', [], {'input': 3, 'cache_read': 7, 'cache_write': 0, 'output': 2},
                          [{'calls': 1}], answer_metadata=metadata)
            path = root / 'out/answer.json'
            self.assertTrue(validate(path))
            answer = json.loads(path.read_text())
            self.assertEqual(answer['status'], 'improvement_claimed')
            self.assertEqual(answer['claim'], metadata['claim'])
            self.assertEqual(answer['claim_type'], 'recipe_improvement')
            self.assertEqual(answer['measurements'][0]['evidence_receipt'], 'evidence/job_score.json')
            self.assertEqual(answer['cost']['model_input_tokens'], 10)
            self.assertEqual(json.loads((root / 'out/reproduction.json').read_text())['argv'], job['lock']['config']['argv'])


class ProspectiveCharter(unittest.TestCase):
    def test_pinned_bar_and_overrides_use_actual_lab_rules(self):
        from eidos.domain.charter import charter_digest, validate_charter
        from eidos.domain.errors import ThresholdOverride
        from eidos.domain.evidence import compile_plan

        from .charters import METRIC, RULE_ID, charter
        d02 = validate_charter(charter('D02'))
        d12 = validate_charter(charter('D12'))
        self.assertEqual(d02['success_rules'], d12['success_rules'])
        self.assertEqual(d02['success_rules'][0]['min_effect'], .02)
        self.assertEqual(d02['success_rules'][0]['metric'], METRIC)
        self.assertTrue(validate_charter(charter('D01'))['exploration_only'])
        args = dict(claim={'kind': 'improves', 'scope': {'task': 'synthetic'}},
                    claim_ref={'id': 'c', 'revision': 1}, charter=d02,
                    charter_ref={'id': 'charter:d02', 'revision': 1, 'digest': charter_digest(d02)},
                    template_ref={'ref': 'template', 'digest': 'sha256:fixture'})
        draft = {'success_rule_id': RULE_ID, 'arms': [{'id': r, 'role': r} for r in ('baseline', 'candidate')]}
        compiled = compile_plan(draft=draft, **args)
        self.assertEqual(compiled['success_rule']['min_effect'], .02)
        with self.assertRaises(ThresholdOverride):
            compile_plan(draft={**draft, 'min_effect': 0}, **args)


if __name__ == '__main__':
    unittest.main()
