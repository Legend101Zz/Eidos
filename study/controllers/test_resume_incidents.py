"""Offline admission tests using a fake launcher and temporary run records."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
from threading import Event, Lock
import tempfile
import unittest
from unittest.mock import patch

from study.controllers import resume_incidents as resume


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runs = self.root / "runs"
        self.markers = self.root / "markers"
        self.history = self.root / "history"
        self.markers.mkdir()
        self.blocks = {
            f"N01-r{run}": [
                {"task": "N01", "run": run, "arm": arm, "block_id": f"N01-r{run}"}
                for arm in range(1, 5)
            ]
            for run in range(1, 5)
        }
        self.admitted = []
        self.now = 1

    def record(self, block, status="incident", complete=True, started_at="start"):
        directory = self.runs / block
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "block_record.json"
        path.write_text(json.dumps({
            "block_id": block, "card": "N01", "run_index": int(block[-1]),
            "status": status, "complete": complete, "stub": False,
            "started_at": started_at, "ended_at": "end",
        }))
        return path

    def fake_run(self, rows, root, *, stub, replace_incident):
        self.assertEqual(root, self.runs)
        self.assertFalse(stub)
        self.assertTrue(replace_incident)
        self.admitted.append(rows[0]["block_id"])

    def dispatch(self, run=None):
        resume.dispatch(self.blocks, self.runs, self.markers, self.history, 10,
                        run or self.fake_run, now=lambda: self.now, free_bytes=lambda: resume.MIN_FREE)

    def test_no_missing_incomplete_or_nonincident_and_old_marker_is_archived(self):
        incident = self.record("N01-r1")
        self.record("N01-r2", complete=False)
        self.record("N01-r3", status="ok")
        (self.markers / "N01-r1").write_text("dispatch marker")
        (self.markers / "N01-r3").write_text("unrelated marker")
        self.dispatch()
        self.assertEqual(self.admitted, ["N01-r1"])
        self.assertFalse((self.markers / "N01-r1").exists())
        self.assertTrue((self.markers / "N01-r3").exists())
        digest = hashlib.sha256(incident.read_bytes()).hexdigest()
        receipt_dir = self.history / "N01-r1" / digest
        self.assertEqual((receipt_dir / "sweep-hosted-marker").read_text(), "dispatch marker")
        self.assertEqual(json.loads((receipt_dir / "admission.json").read_text())["previous_started_at"], "start")

    def test_deadline_at_entry_skips_everything(self):
        self.record("N01-r1")
        self.now = 10
        self.dispatch()
        self.assertEqual(self.admitted, [])
        self.assertFalse(self.history.exists())

    def test_cutoff_during_marker_io_restores_marker(self):
        self.record("N01-r1")
        marker = self.markers / "N01-r1"
        marker.write_text("keep")
        original_archive = resume.archive_marker

        def archive_then_expire(*args):
            result = original_archive(*args)
            self.now = 10
            return result

        with patch.object(resume, "archive_marker", archive_then_expire):
            self.dispatch()
        self.assertEqual(self.admitted, [])
        self.assertEqual(marker.read_text(), "keep")
        self.assertEqual(len(list(self.history.rglob("not-admitted"))), 1)

    def test_status_changed_after_discovery_is_rechecked(self):
        for block in self.blocks:
            self.record(block)
        both_started = Event()
        release = Event()
        active = set()
        guard = Lock()
        admitted_peak = 0

        def runner(rows, root, **kwargs):
            nonlocal admitted_peak
            self.fake_run(rows, root, **kwargs)
            block = rows[0]["block_id"]
            with guard:
                active.add(block)
                admitted_peak = max(admitted_peak, len(active))
                if len(active) == 2:
                    both_started.set()
            try:
                self.assertTrue(release.wait(5))
            finally:
                with guard:
                    active.remove(block)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.dispatch, runner)
            try:
                self.assertTrue(both_started.wait(5))
                self.assertEqual(set(self.admitted), {"N01-r1", "N01-r2"})
                self.record("N01-r3", status="ok")
                self.record("N01-r4", started_at="another-attempt")
            finally:
                release.set()
            future.result(timeout=5)
        self.assertCountEqual(self.admitted, ["N01-r1", "N01-r2"])
        self.assertEqual(admitted_peak, 2)

    def test_expired_queued_candidates_never_start(self):
        for block in self.blocks:
            self.record(block)
        both_started = Event()
        release = Event()
        guard = Lock()

        def runner(rows, root, **kwargs):
            with guard:
                self.fake_run(rows, root, **kwargs)
                if len(self.admitted) == 2:
                    both_started.set()
            self.assertTrue(release.wait(5))

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.dispatch, runner)
            try:
                self.assertTrue(both_started.wait(5))
                self.now = 10
            finally:
                release.set()
            future.result(timeout=5)
        self.assertCountEqual(self.admitted, ["N01-r1", "N01-r2"])

    def test_design_refuses_changed_bytes(self):
        path = self.root / "design.json"
        path.write_text("[]")
        with self.assertRaisesRegex(ValueError, "original Pilot 3"):
            resume.read_design(path)


if __name__ == "__main__":
    unittest.main()
