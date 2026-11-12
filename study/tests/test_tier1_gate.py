"""Offline Tier-1 evidence and readiness regression tests."""

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from study.eidos_arms import lab
from study.evals.compare import compare
from study.evals.mini_eval import SCHEMA, snapshot_references, summarize
from study.evals.policy import POLICY
from study.evals.provenance import MIN_FREE, disk_guard, verify_build
from study.evals.provider_audit import RequestAudit
from study.evals.records import inspect_run
from study.evals.regenerate_references import CARDS, regenerate
from study.tests.test_provider_audit_dispatch import audit_fixture


class GateTests(unittest.TestCase):
    def scorecard(self):
        rows = [
            {
                "card": card,
                "arm": 4,
                "seed_index": 1,
                "status": "valid",
                "run_status": "ok",
                "verified_success": True,
                "concluded": True,
                "disposition": "investigator_conclusion",
                "verified_test_gain": 0.25,
                "gain_ratio": None if card == "D02" else 0.5,
                "reference_median": None if card == "D02" else 0.5,
                "invariant_flags": dict.fromkeys(
                    ("dead_end", "fabricated_empty_wait", "held_out_reads", "runtime_deadline_limit"), False
                ),
            }
            for card in CARDS
        ]
        return {
            "schema_version": SCHEMA,
            "comparison_policy": POLICY,
            "cards": list(CARDS),
            "arms": [4],
            "seed_index": 1,
            "envelope": {},
            "model": "fixture",
            "stub": False,
            "concurrency": 2,
            "runs": rows,
            "aggregate": summarize(rows),
            "by_arm": {"4": summarize(rows)},
            "tier1": dict.fromkeys(
                (
                    "full_development_set",
                    "completion_pass",
                    "invariant_pass",
                    "run_evidence_pass",
                    "eligible",
                ),
                True,
            ),
            "provenance": {
                "git_sha": "a" * 40,
                "design_sha256": "b" * 64,
                "git_dirty": False,
                "reference_gains_sha256": "a" * 64,
                "provider_audit_identifiers_sha256": "b" * 64,
                "expected_build_sha": "b" * 40,
                "pilot_build_sha256": "c" * 64,
                "pilot_build": {"eidos_commit_sha": "b" * 40},
            },
        }

    def test_same_aggregate_does_not_hide_lost_verification_or_gain(self):
        old = self.scorecard()
        new = copy.deepcopy(old)
        new["runs"][0].update(verified_success=False, verified_test_gain=None, gain_ratio=0.0)
        d02 = next(row for row in new["runs"] if row["card"] == "D02")
        d02["verified_test_gain"] -= 0.000001
        result = compare(old, new)
        self.assertFalse(result["gate_pass"])
        self.assertTrue(any("N01 arm 4 verified_success" in issue for issue in result["regressions"]))
        self.assertTrue(any("D02 arm 4 verified_test_gain" in issue for issue in result["regressions"]))
        with self.assertRaisesRegex(ValueError, "tolerance zero"):
            compare(old, new, 0.05)

    def test_required_unknown_and_positive_fail_but_deadline_is_diagnostic(self):
        old = self.scorecard()
        new = copy.deepcopy(old)
        new["runs"][0]["invariant_flags"]["runtime_deadline_limit"] = True
        self.assertTrue(compare(old, new)["gate_pass"])
        new["runs"][0]["invariant_flags"]["held_out_reads"] = None
        result = compare(old, new)
        self.assertFalse(result["gate_pass"])
        self.assertTrue(any("held_out_reads evidence is unknown" in issue for issue in result["blockers"]))
        new["runs"][0]["invariant_flags"]["held_out_reads"] = True
        new["runs"][0]["invariant_flags"]["fabricated_empty_wait"] = True
        result = compare(old, new)
        self.assertEqual(len(result["regressions"]), 2)
        self.assertFalse(result["gate_pass"])

    def test_optional_arm2_keeps_unknown_coverage_outside_required_gate(self):
        before = self.scorecard()
        before["arms"] = [2, 4]
        optional = [
            {
                **row,
                "arm": 2,
                "disposition": "coding_answer",
                "invariant_flags": dict.fromkeys(row["invariant_flags"]),
            }
            for row in before["runs"]
        ]
        before["runs"].extend(optional)
        before["by_arm"]["2"] = summarize(optional)
        before["aggregate"] = summarize(before["runs"])
        after = copy.deepcopy(before)
        self.assertTrue(compare(before, after)["gate_pass"])
        self.assertEqual(after["by_arm"]["2"]["invariant_unknown_counts"]["held_out_reads"], len(CARDS))
        after["runs"][0]["invariant_flags"]["held_out_reads"] = None
        self.assertFalse(compare(before, after)["gate_pass"])
        after["runs"][0]["invariant_flags"]["held_out_reads"] = False
        after["runs"][-1]["verified_test_gain"] -= 0.01
        self.assertTrue(
            any("arm 2 verified_test_gain" in issue for issue in compare(before, after)["regressions"])
        )

    def test_complete_reference_is_hash_bound_and_preserves_d02_null(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = [
                {
                    "task": card,
                    "arm": arm,
                    "run": run,
                    "status": "valid",
                    "run_status": "ok",
                    "verified_success": True,
                    "numeric": {"gain": 0.1},
                }
                for card in CARDS[:4]
                for arm in range(1, 5)
                for run in range(1, 4)
            ]
            hashes = {}
            for name in ("design.json", "results-record.json"):
                raw = json.dumps(results).encode()
                (root / name).write_bytes(raw)
                hashes[name] = hashlib.sha256(raw).hexdigest()
            (root / "manifest.json").write_text(json.dumps({"complete": True, "output_sha256": hashes}))
            pilot2 = root / "pilot2.json"
            pilot2.write_text("[]")
            value = regenerate(root, pilot2)
            self.assertIsNone(value["cards"]["D02"]["median"])
            self.assertEqual(value["cards"]["N01"]["n"], 9)
            reference = root / "reference.json"
            reference.write_text(json.dumps(value))
            self.assertEqual(
                snapshot_references(reference, hashlib.sha256(reference.read_bytes()).hexdigest(), live=True),
                reference.read_bytes(),
            )
            with self.assertRaisesRegex(ValueError, "explicit hash"):
                snapshot_references(reference, "x", live=True)
            (root / "results-record.json").write_text("[]")
            with self.assertRaisesRegex(ValueError, "artifact hash differs"):
                regenerate(root, pilot2)

    def test_audit_config_is_not_passed_to_lab_or_candidate_jobs(self):
        with patch.dict(os.environ, {"EIDOS_EVAL_AUDIT_IDENTIFIERS": "/controller/audit.json"}):
            self.assertNotIn("EIDOS_EVAL_AUDIT_IDENTIFIERS", lab.environment())

    def test_provider_audit_retains_only_digests_and_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            known = "a" * 64
            root, config, _ = audit_fixture(Path(directory), "N01", known)
            root.mkdir(parents=True)
            (root / "arm.json").write_text('{"arm":4,"card":"N01","run_index":1,"block_id":"N01-r1"}')
            state = root / "provider_bridge.json"
            audit = RequestAudit(config, state)
            request = json.dumps({"messages": [{"content": "transient-body " + known}]}).encode()
            audit.scan(request)
            saved = (root / "eval_provider_audit.jsonl").read_text()
            self.assertNotIn("transient-body", saved)
            self.assertEqual(
                json.loads(saved),
                {
                    "request_sha256": hashlib.sha256(request).hexdigest(),
                    "matched_identifiers": [known],
                },
            )

    def test_audit_requires_context_and_requests_and_excludes_internal_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            known = "a" * 64
            root, config, sha = audit_fixture(Path(directory), "D02", known)
            root.mkdir(parents=True)
            (root / "arm.json").write_text('{"arm":4,"card":"D02","run_index":1,"block_id":"D02-r1"}')
            state = root / "provider_bridge.json"
            state.write_text(json.dumps({"eval_provider_audit": True, "eval_provider_audit_config_sha256": sha}))
            (root / "run_record.json").write_text(
                json.dumps(
                    {
                        "arm": 4,
                        "card": "D02",
                        "status": "ok",
                        "lab_home": str(root),
                    }
                )
            )

            def artifact(subject, schema, raw):
                digest = hashlib.sha256(raw).hexdigest()
                path = root / "artifacts/blobs/sha256" / digest[:2] / digest
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
                return {
                    "type": "ledger_event",
                    "data": {
                        "type": "artifact.registered",
                        "subject": subject,
                        "body": {"schema": schema, "digest": digest},
                    },
                }

            rows = [
                artifact("packed", "packed-context/1", b"visible safe context"),
                artifact("internal", "investigation-view/1", known.encode()),
                {"type": "ledger_event", "data": {"type": "model.usage", "body": {"usage": {"calls": 1}}}},
                {
                    "type": "turn",
                    "data": {"id": "turn", "context": {"context_artifact_id": "packed"}, "decisions": []},
                },
                {
                    "type": "investigation",
                    "data": {
                        "status": "concluded",
                        "turns": [{"id": "turn"}],
                        "body": {"conclusion": {"summary": "done"}},
                    },
                },
            ]
            transcript = root / "transcript.jsonl"
            transcript.write_text("\n".join(map(json.dumps, rows)))
            self.assertIsNone(inspect_run(root, [known], audit_config_sha=sha)["invariant_flags"]["held_out_reads"])
            audit = RequestAudit(config, state)
            audit.scan(b'{"messages":[{"content":"visible safe context"}]}')
            self.assertIs(inspect_run(root, [known], audit_config_sha=sha)["invariant_flags"]["held_out_reads"], False)
            audit.scan(json.dumps({"messages": [{"content": "actual tool response " + known}]}).encode())
            self.assertIs(inspect_run(root, [known], audit_config_sha=sha)["invariant_flags"]["held_out_reads"], True)

    def test_locked_silent_stop_and_unavailable_audit_are_not_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "run_record.json").write_text('{"arm":4,"card":"D02","status":"timeout"}')
            rows = [
                {"type": "investigation", "data": {"status": "stopped"}},
                {"type": "ledger_event", "data": {"type": "incumbent.changed", "body": {}}},
            ]
            (root / "transcript.jsonl").write_text("\n".join(map(json.dumps, rows)))
            observation = inspect_run(root)
            self.assertTrue(observation["invariant_flags"]["dead_end"])
            self.assertIsNone(observation["invariant_flags"]["held_out_reads"])
            self.assertFalse(observation["concluded"])

    def test_disk_preflight_uses_exact_operational_floor(self):
        with patch("study.evals.provenance.shutil.disk_usage") as usage:
            usage.return_value.free = MIN_FREE - 1
            with self.assertRaisesRegex(RuntimeError, "12.5 GiB"):
                disk_guard()
            usage.return_value.free = MIN_FREE
            disk_guard()

    def test_build_preflight_refuses_unexpected_or_unknown_provenance(self):
        fake_home = Path("/tmp").resolve() / "build/home"
        python = fake_home.parent / "venv/bin/python"
        for expected in (None, "short"):
            with self.assertRaisesRegex(ValueError, "explicit full"):
                verify_build(Path("."), fake_home, python, expected, {})
        with self.assertRaisesRegex(ValueError, "differs"):
            verify_build(Path("."), fake_home, python, "a" * 40, {"eidos_commit_sha": "b" * 40})
        process = unittest.mock.MagicMock()
        process.stdout = unittest.mock.MagicMock()
        process.wait.return_value = 0
        with (
            patch("study.evals.provenance.EVAL_ROOT", fake_home.parent.parent),
            patch("study.evals.provenance.subprocess.Popen", return_value=process),
            patch("study.evals.provenance.hashlib.file_digest") as digest,
            self.assertRaisesRegex(ValueError, "source archive provenance"),
        ):
            digest.return_value.hexdigest.return_value = "a" * 64
            verify_build(
                Path("."), fake_home, python, "a" * 40, {"eidos_commit_sha": "a" * 40, "python": str(python)}
            )

    def test_g4c_controller_abstention_is_distinct_from_investigator_conclusion(self):
        fixture_path = Path(__file__).resolve().parents[1] / "evals/fixtures/controller_abstention.json"
        fixture = json.loads(fixture_path.read_text())
        state = fixture["state"]
        receipt = state["body"]["controller_abstention"]
        self.assertEqual(fixture["events"][0]["body"], receipt)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "run_record.json").write_text('{"arm":4,"card":"D02","status":"crash"}')
            rows = [
                {"type": "ledger_event", "data": {"type": "incumbent.changed", "body": {}}},
                {"type": "ledger_event", "data": fixture["events"][0]},
                {"type": "turn", "data": {"id": state["turns"][0]["id"], "decisions": []}},
            ]
            transcript = root / "transcript.jsonl"

            def write_state(value):
                transcript.write_text(
                    "\n".join(map(json.dumps, [*rows, {"type": "investigation", "data": value}]))
                )

            write_state(state)
            observed = inspect_run(root)
            self.assertFalse(observed["concluded"])
            self.assertEqual(observed["controller_abstention"], receipt)
            self.assertEqual(observed["disposition"], "controller_abstention")
            self.assertFalse(observed["invariant_flags"]["dead_end"])
            write_state(
                {
                    "status": "concluded",
                    "body": {
                        "conclusion": {"scope": {"answer": {"status": "abstained"}}},
                    },
                }
            )
            observed = inspect_run(root)
            self.assertTrue(observed["concluded"])
            self.assertEqual(observed["disposition"], "investigator_abstention")
