import json
import sys
import tempfile
import unittest
from pathlib import Path

from study.common import write_json
from study.run_arm import (
    UsageTracker, _is_legacy_rate_limit_echo, _legacy_rate_limit_text, event_error, incident_text, run_stream,
)


class UsageTests(unittest.TestCase):
    def test_repeated_messages_and_cumulative_result_are_not_double_counted(self):
        tracker = UsageTracker()
        for output in (2, 7, 7):
            tracker.observe(
                dict(
                    type="assistant",
                    message=dict(
                        id="one",
                        model="shared",
                        usage=dict(
                            input_tokens=10,
                            cache_read_input_tokens=100,
                            cache_creation_input_tokens=20,
                            output_tokens=output,
                        ),
                    ),
                )
            )
        tracker.observe(
            dict(
                type="assistant",
                message=dict(id="two", model="shared", usage=dict(input_tokens=5, output_tokens=3)),
            )
        )
        tracker.observe(
            dict(
                type="result",
                result="Exact final claim.",
                usage=dict(
                    input_tokens=15,
                    cache_read_input_tokens=100,
                    cache_creation_input_tokens=20,
                    output_tokens=10,
                ),
            )
        )
        self.assertEqual(tracker.tokens, dict(input=15, cache_read=100, cache_write=20, output=10, total=145))
        self.assertEqual(tracker.claim, "Exact final claim.")
        self.assertEqual(tracker.models, {"shared"})

    def test_provider_errors_and_unrelated_numbers(self):
        for text in (
            "API Error: 503 unavailable",
            '{"status_code": 529}',
            "quota exhausted",
            "HTTP 500",
            "429",
        ):
            self.assertTrue(incident_text(text), text)
        self.assertFalse(incident_text("EPERM open /tmp/claude-501"))
        self.assertEqual(event_error(dict(type="assistant", message={"content": "quota is a metric"})), "")

    def test_partial_stream_counts_usage_before_the_final_result(self):
        tracker = UsageTracker()
        tracker.observe(
            dict(
                type="stream_event",
                event=dict(
                    type="message_start",
                    message=dict(
                        id="partial", model="shared", usage=dict(input_tokens=10, cache_read_input_tokens=50)
                    ),
                ),
            )
        )
        tracker.observe(
            dict(type="stream_event", event=dict(type="message_delta", usage=dict(output_tokens=7)))
        )
        self.assertEqual(tracker.tokens["total"], 67)
        tracker.observe(
            dict(
                type="assistant",
                message=dict(id="partial", model="shared", usage=dict(input_tokens=0, output_tokens=0)),
            )
        )
        self.assertEqual(tracker.tokens["total"], 67)

    def test_model_usage_summary_camelcase(self):
        tracker = UsageTracker()
        tracker.observe(
            dict(
                type="result",
                result="done",
                modelUsage={
                    "shared": {
                        "inputTokens": 10,
                        "cacheReadInputTokens": 20,
                        "cacheCreationInputTokens": 30,
                        "outputTokens": 5,
                    }
                },
            )
        )
        self.assertEqual(tracker.tokens["total"], 65)


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
        record = self.run_code("import time;time.sleep(30)", wall_s=0.1)
        self.assertEqual(record["status"], "timeout")
        self.assertLess(record["wall_s"], 3)

    def test_cached_tokens_stop_a_running_process(self):
        event = dict(
            type="assistant", message=dict(id="m", model="shared", usage=dict(cache_read_input_tokens=101))
        )
        code = f"import time;print({json.dumps(event)!r},flush=True);time.sleep(30)"
        record = self.run_code(code, wall_s=5, token_limit=100)
        self.assertEqual(record["status"], "token_budget")
        self.assertEqual(record["tokens"]["total"], 101)

    def test_quota_error_is_incident(self):
        code = 'print(\'{"type":"result","is_error":true,"errors":["API Error: 429 quota exhausted"]}\')'
        record = self.run_code(code)
        self.assertEqual(record["status"], "incident")
        self.assertTrue((self.root / "transcript.jsonl").exists())

    def run_events(self, *events, stall=False, **limits):
        code = "".join(f"print({json.dumps(event)!r}, flush=True)\n" for event in events)
        if stall:
            code += "import time; time.sleep(30)\n"
        return self.run_code(code, **limits)

    provider_failure = {"type": "error", "error": "HTTP 429 rate limit"}
    response = {"type": "assistant", "message": {
        "id": "recovered", "model": "fixture", "usage": {"input_tokens": 10, "output_tokens": 2},
    }}
    local_failure = {"type": "result", "subtype": "error_during_execution", "is_error": True,
                     "errors": ["local execution failed"]}

    def test_completed_provider_response_clears_prior_incident(self):
        for ending in ((), (self.local_failure,)):
            with self.subTest(ending=ending):
                record = self.run_events(self.provider_failure, self.response, *ending)
                self.assertEqual(record["status"], "crash")
                self.assertIsNone(record["incident_reason"])

    def test_terminal_provider_failure_after_recovery_is_incident(self):
        for message in ("HTTP 503 service unavailable", "HTTP 429 rate limit", "quota exhausted"):
            with self.subTest(message=message):
                terminal = {**self.local_failure, "errors": [message]}
                record = self.run_events(self.provider_failure, self.response, terminal)
                self.assertEqual(record["status"], "incident")
                self.assertIn(message, record["incident_reason"])

    def test_terminal_provider_status_code_is_incident(self):
        terminal = {"type": "result", "is_error": True, "status_code": 503}
        record = self.run_events(self.response, terminal)
        self.assertEqual(record["status"], "incident")
        self.assertIn("503", record["incident_reason"])

    def test_retry_tool_partial_and_synthetic_events_do_not_clear_incident(self):
        messages = (
            {"type": "system", "subtype": "api_retry", "error": "HTTP 503 service unavailable"},
            {"type": "user", "is_error": True, "error": "local tool failed"},
            {"type": "stream_event", "event": {"type": "message_start", "message": self.response["message"]}},
            {**self.response, "message": {**self.response["message"], "model": "<synthetic>"}},
        )
        for event in messages:
            with self.subTest(event=event):
                record = self.run_events(self.provider_failure, event)
                self.assertEqual(record["status"], "incident")
                self.assertIn("HTTP 429 rate limit", record["incident_reason"])
                record = self.run_events(self.provider_failure, event, self.local_failure)
                self.assertEqual(record["status"], "crash")
                self.assertIsNone(record["incident_reason"])

    def test_tool_error_with_provider_words_does_not_create_incident(self):
        tool = {"type": "user", "is_error": True, "error": "Tool output: quota HTTP 503"}
        record = self.run_events(tool)
        self.assertEqual(record["status"], "crash")
        self.assertIsNone(record["incident_reason"])

    def test_synthetic_assistant_explicit_api_error_can_record_incident(self):
        provider = {"type": "assistant", "error": "rate_limit",
                    "message": {"id": "synthetic-error", "model": "<synthetic>"}}
        record = self.run_events(provider)
        self.assertEqual(record["status"], "incident")
        self.assertIn("rate_limit", record["incident_reason"])

    def test_typed_error_message_can_record_provider_incident(self):
        provider = {"type": "error", "message": "HTTP 503 service unavailable"}
        record = self.run_events(provider)
        self.assertEqual(record["status"], "incident")
        self.assertIn("503", record["incident_reason"])

    def test_legacy_cli_echo_requires_exact_typed_rate_limit_corroboration(self):
        text = "API Error: rate limit from provider"
        marker = {"type": "assistant", "error": "rate_limit", "message": {
            "id": "rate-limit", "model": "<synthetic>", "content": [{"type": "text", "text": text}],
        }}
        final = {"type": "result", "subtype": "success", "is_error": True, "result": text}
        self.assertEqual(_legacy_rate_limit_text(marker), text)
        self.assertTrue(_is_legacy_rate_limit_echo(final, text))
        self.assertFalse(_is_legacy_rate_limit_echo({**final, "errors": []}, text))
        self.assertIsNone(_legacy_rate_limit_text({**marker, "type": "user"}))

        for name, events, status in (
            ("exact", (marker, final), "incident"),
            ("mismatch", (marker, {**final, "result": "API Error: HTTP 503"}), "crash"),
            ("scientific quota", (marker, {**final, "result": "quota of samples is too small"}), "crash"),
            ("local subtype", (marker, {**final, "subtype": "error_during_execution"}), "crash"),
            ("recovered", (marker, self.response, final), "crash"),
            ("replaced", (marker, self.provider_failure, final), "crash"),
            ("tool spoof", ({**marker, "type": "user"}, final), "crash"),
            ("explicit local", (marker, {**final, "errors": ["local failure"]}), "crash"),
            ("explicit provider", (marker, {**final, "status_code": 503}), "incident"),
        ):
            with self.subTest(name=name):
                record = self.run_events(*events)
                self.assertEqual(record["status"], status)
                if name == "explicit provider":
                    self.assertIn("503", record["incident_reason"])

    def test_unattributed_stderr_does_not_create_incident(self):
        for message in ("quota of samples is too small", "HTTP 429 rate limit"):
            with self.subTest(message=message):
                record = self.run_code(f"import sys; print({message!r}, file=sys.stderr)")
                self.assertEqual(record["status"], "crash")
                self.assertIsNone(record["incident_reason"])
                self.assertIn(message, (self.root / "transcript.jsonl").read_text())

    def test_repeated_assistant_message_does_not_clear_new_incident(self):
        record = self.run_events(self.response, self.provider_failure, self.response)
        self.assertEqual(record["status"], "incident")
        self.assertIn("HTTP 429 rate limit", record["incident_reason"])

    def test_explicit_unknown_result_does_not_inherit_provider_error(self):
        unknown = {"type": "result", "subtype": "error_during_execution", "is_error": True}
        for ending in (self.local_failure, unknown):
            with self.subTest(ending=ending):
                record = self.run_events(self.provider_failure, ending)
                self.assertEqual(record["status"], "crash")
                self.assertIsNone(record["incident_reason"])

    def test_terminal_local_error_ignores_quota_words_outside_error_fields(self):
        terminal = {**self.local_failure, "result": "Prose about quota and HTTP 503.", "note": "429"}
        record = self.run_events(self.provider_failure, terminal)
        self.assertEqual(record["status"], "crash")
        self.assertIsNone(record["incident_reason"])
        self.assertIn(terminal, [json.loads(line) for line in (self.root / "transcript.jsonl").read_text().splitlines()])

    def test_retry_notice_alone_is_not_a_terminal_provider_failure(self):
        retry = {"type": "system", "subtype": "api_retry", "error": "HTTP 503 service unavailable"}
        record = self.run_events(retry, self.local_failure)
        self.assertEqual(record["status"], "crash")
        self.assertIsNone(record["incident_reason"])

    def test_success_and_run_bounds_keep_priority_over_provider_errors(self):
        success = {"type": "result", "subtype": "success", "result": "finished"}
        record = self.run_events(self.provider_failure, success)
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["agent_claim"], "finished")
        record = self.run_events(self.provider_failure, stall=True, wall_s=0.1)
        self.assertEqual(record["status"], "timeout")
        record = self.run_events(self.provider_failure, self.response, stall=True, token_limit=11)
        self.assertEqual(record["status"], "token_budget")

    def test_child_key_is_redacted_from_transcript_and_claim(self):
        import os

        # A synthetic sentinel, not a credential or a Keychain read.
        sentinel = "synthetic-secret-for-redaction-test"
        code = 'import json,os;print(json.dumps(dict(type="result", result=os.environ["ANTHROPIC_API_KEY"])))'
        record = run_stream(
            self.root, [sys.executable, "-u", "-c", code], dict(os.environ, ANTHROPIC_API_KEY=sentinel)
        )
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["agent_claim"], "[REDACTED]")
        self.assertNotIn(sentinel, (self.root / "transcript.jsonl").read_text())


if __name__ == "__main__":
    unittest.main()
