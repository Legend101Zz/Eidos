"""Tier-1 baseline-stub qualification and portable real-record replay extraction."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from study.common import ROOT, read_json, write_json
from study.evals.compare import compare
from study.evals.extract_replay import SCHEMA as REPLAY_SCHEMA
from study.evals.extract_replay import extract
from study.evals.mini_eval import SCHEMA, run_eval
from study.evals.records import inspect_run

FIXTURES = ROOT / "study/evals/fixtures"


class MiniEval(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("EIDOS_RESEARCH_PYTHON"), "requires the study research interpreter")
    def test_two_card_stub_end_to_end_and_compare_cli(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ):
            root = Path(directory)
            python = root / "venv/bin/python"
            python.parent.mkdir(parents=True)
            python.symlink_to(sys.executable)
            write_json(root / "venv/pilot-build.json", dict(eidos_commit_sha="stub-build", python=str(python)))
            # Exercise the public CLI, research subprocesses, optional arm 2 and local grading.
            command = [sys.executable, "-m", "study.evals.mini_eval", "--cards", "N01,N03", "--arms", "2,4",
                       "--seed-index", "2", "--out", str(root / "out"), "--lab-home", str(root / "home"),
                       "--lab-python", str(python), "--stub"]
            process = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=240)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            score = read_json(root / "out/scorecard.json")
            self.assertEqual(score["schema_version"], SCHEMA)
            self.assertEqual(score["aggregate"]["runs"], 4)
            self.assertEqual(score["aggregate"]["concluded"], 4)
            self.assertEqual(score["aggregate"]["verified"], 0)
            self.assertEqual(score["aggregate"]["tokens"], 0)
            self.assertFalse(score["tier1"]["eligible"])
            self.assertFalse(score["tier1"]["completion_pass"])
            for row in score["runs"]:
                self.assertEqual(row["status"], "valid")
                self.assertEqual(row["test_gain"], 0)
                self.assertEqual(row["gain_ratio"], 0)
                self.assertEqual(row["seed_index"], 2)
                self.assertGreater(row["unused_time_at_conclusion_s"], 0)
                self.assertFalse(any(row["invariant_flags"].values()))
            design = read_json(root / "out/design.json")
            self.assertEqual({r["arm"] for r in design}, {2, 4})
            self.assertEqual({r["block_id"] for r in design}, {"N01-r2", "N03-r2"})
            self.assertEqual(len(list((root / "out/grading").glob("*/arm*/grade.json"))), 4)
            self.assertIn("N01", (root / "out/scorecard.md").read_text())
            self.assertIn("pilot_build_sha256", score["provenance"])
            self.assertEqual(score["provenance"]["pilot_build"]["eidos_commit_sha"], "stub-build")
            self.assertFalse(compare(score, score)["regression"])
            for metric, value in (("concluded", 3), ("dead_ends", 1), ("median_gain_ratio", -0.1)):
                regressed = copy.deepcopy(score)
                regressed["aggregate"][metric] = value
                write_json(root / "regressed.json", regressed)
                result = subprocess.run([sys.executable, "-m", "study.evals.compare", str(root / "out/scorecard.json"),
                                         str(root / "regressed.json")], cwd=ROOT, text=True, capture_output=True)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertTrue(json.loads(result.stdout)["regression"])
            self.assertFalse(compare(score, score, tolerance=0)["regression"])
            with self.assertRaisesRegex(ValueError, "output directory must be empty"):
                run_eval(["N01"], (4,), 1, root / "out", root / "home", python, stub=True)


class ReplayExtraction(unittest.TestCase):
    def materialize_real_record(self, root):
        """Recreate the source shape from the committed real record, preserving blob hashes."""
        expected = read_json(FIXTURES / "N01-r1.json")
        run = root / "run"
        run.mkdir()
        home = root / "lab"
        record = dict(expected["run_record"], lab_home=str(home))
        write_json(run / "run_record.json", record)
        write_json(run / "investigation_latest.json", expected["final_state"])
        events = {event["seq"]: event for event in expected["receipts"]}
        for artifact in expected["response_artifacts"]:
            digest = artifact["sha256"]
            raw = json.dumps(artifact["messages"], default=str, sort_keys=True).encode()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
            path = home / "artifacts/blobs/sha256" / digest[:2] / digest
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            events[artifact["ledger_seq"]] = dict(
                seq=artifact["ledger_seq"], type="artifact.registered", subject=artifact["artifact_id"],
                body=dict(name=artifact["name"], digest=digest, schema="model-response/1"))
        rows = [dict(type="ledger_event", data=e) for _, e in sorted(events.items())]
        rows.extend(dict(type="turn", data=dict(t, id=t["turn_id"])) for t in expected["turns"])
        (run / "transcript.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        return run, expected

    def test_extract_one_real_record_including_unexported_replies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run, expected = self.materialize_real_record(root)
            result = subprocess.run([sys.executable, "-m", "study.evals.extract_replay", str(run), str(root / "fixture.json")],
                                    cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            fixture = read_json(root / "fixture.json")
            self.assertEqual(fixture["schema_version"], REPLAY_SCHEMA)
            self.assertEqual(fixture["requests"], expected["requests"])
            self.assertEqual(fixture["final_state"], expected["final_state"])
            self.assertEqual(len(fixture["response_artifacts"]), 19)
            self.assertEqual(len(fixture["requests"]), 41)
            # Only 12 accepted decisions were exported by the original arm adapter.
            self.assertGreater(len(fixture["response_artifacts"]), sum(a["accepted"] is True for a in fixture["response_artifacts"]))
            self.assertTrue(fixture["needs_input"])
            self.assertIn("invalid-proposals", {t["stop_reason"] for t in fixture["turns"]})
            observations = inspect_run(run)
            self.assertTrue(observations["invariant_flags"]["dead_end"])
            self.assertTrue(observations["invariant_flags"]["fabricated_empty_wait"])
            self.assertTrue(observations["invariant_flags"]["runtime_deadline_limit"])
            artifact = fixture["response_artifacts"][0]
            digest = artifact["sha256"]
            blob = root / "lab/artifacts/blobs/sha256" / digest[:2] / digest
            blob.write_text("{}")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                extract(run)

    def test_all_real_fixtures_have_ordered_request_timings(self):
        for block in ("N01-r1", "N01-r2", "N03-r1", "N04-r2"):
            fixture = read_json(FIXTURES / (block + ".json"))
            self.assertEqual(fixture["schema_version"], REPLAY_SCHEMA)
            self.assertEqual(fixture["source"]["block_id"], block)
            self.assertTrue(fixture["needs_input"])
            requests = fixture["requests"]
            self.assertEqual([r["index"] for r in requests], list(range(len(requests))))
            self.assertEqual([r["started_unix"] for r in requests], sorted(r["started_unix"] for r in requests))
            self.assertEqual(len({r["provider_response_id"] for r in requests}), len(requests))
            self.assertTrue(all(r["elapsed_s"] >= 0 and r["remaining_s"] >= 0 for r in requests))
