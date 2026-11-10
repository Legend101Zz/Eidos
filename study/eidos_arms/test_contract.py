"""Focused accounting, selection and incident regression checks; no model calls."""
import json
from pathlib import Path
import tempfile
import unittest

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


if __name__ == '__main__':
    unittest.main()
