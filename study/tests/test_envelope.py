import json
from pathlib import Path
import sys
import tempfile
import unittest

from study.common import write_json
from study.run_arm import UsageTracker, event_error, incident_text, run_stream


class UsageTests(unittest.TestCase):
    def test_repeated_messages_and_cumulative_result_are_not_double_counted(self):
        tracker = UsageTracker()
        for output in (2, 7, 7):
            tracker.observe(dict(type="assistant", message=dict(id="one", model="shared", usage=dict(
                input_tokens=10, cache_read_input_tokens=100, cache_creation_input_tokens=20, output_tokens=output))))
        tracker.observe(dict(type="assistant", message=dict(id="two", model="shared", usage=dict(input_tokens=5, output_tokens=3))))
        tracker.observe(dict(type="result", result="Exact final claim.", usage=dict(input_tokens=15,
                             cache_read_input_tokens=100, cache_creation_input_tokens=20, output_tokens=10)))
        self.assertEqual(tracker.tokens, dict(input=15, cache_read=100, cache_write=20, output=10, total=145))
        self.assertEqual(tracker.claim, "Exact final claim.")
        self.assertEqual(tracker.models, {"shared"})

    def test_provider_errors_and_unrelated_numbers(self):
        for text in ("API Error: 503 unavailable", '{"status_code": 529}', "quota exhausted", "HTTP 500", "429"):
            self.assertTrue(incident_text(text), text)
        self.assertFalse(incident_text("EPERM open /tmp/claude-501"))
        self.assertEqual(event_error(dict(type="assistant", message={"content": "quota is a metric"})), "")


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".test-tmp"
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name)
        write_json(self.root / "arm.json", dict(arm=1, card="TOY", run_index=1, block_id="test"))

    def tearDown(self):
        self.temp.cleanup()

    def run_code(self, code, **limits):
        import os
        return run_stream(self.root, [sys.executable, "-u", "-c", code], dict(os.environ), **limits)

    def test_timeout_is_enforced_without_stream_output(self):
        record = self.run_code("import time;time.sleep(30)", wall_s=.1)
        self.assertEqual(record["status"], "timeout")
        self.assertLess(record["wall_s"], 3)

    def test_cached_tokens_stop_a_running_process(self):
        event = dict(type="assistant", message=dict(id="m", model="shared", usage=dict(cache_read_input_tokens=101)))
        code = f"import time;print({json.dumps(event)!r},flush=True);time.sleep(30)"
        record = self.run_code(code, wall_s=5, token_limit=100)
        self.assertEqual(record["status"], "token_budget")
        self.assertEqual(record["tokens"]["total"], 101)

    def test_quota_error_is_incident(self):
        code = 'print(\'{"type":"result","is_error":true,"errors":["API Error: 429 quota exhausted"]}\')'
        record = self.run_code(code)
        self.assertEqual(record["status"], "incident")
        self.assertTrue((self.root / "transcript.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
