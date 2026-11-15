"""Offline selected-arm contracts and byte comparisons with frozen historical functions.

Only fabricated outcomes and committed public Pilot 3 fixtures are used. The frozen
functions let figures be compared under the same plotting library, without requiring
Git or fixing PNG hashes to one Matplotlib/platform version.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import numpy as np
from research.public.development import analyze_results as estimator

from study import analyze as descriptive
from study import grade_block as grading
from study import launcher
from study.common import ROOT, read_json, write_json
from study.eidos_arms import charters

FIXTURES = Path(__file__).parent / "fixtures/four_arm_782d6c8"
HISTORY = ROOT / "study/packaging/evidence/p3"


def legacy(name, module, function):
    namespace = dict(vars(module))
    exec(compile((FIXTURES / f"{name}.py.txt").read_text(), str(FIXTURES / name), "exec"), namespace)
    return namespace[function]


def roster(arms=(2, 4)):
    return launcher.plan(20261113, ["N01", "N03", "N04", "D02"], 6, arms)


def outcomes(design):
    return [dict(row, status="valid", verified_success=(row["run"] + row["arm"]) % 3 == 0,
                 billed_usd=None, tokens={"total": row["run"] * 10}, wall_s=float(row["run"]))
            for row in design]


class SelectedArmPlanning(unittest.TestCase):
    def invoke(self, *args):
        with patch.object(sys, "argv", ["launcher", *map(str, args)]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            launcher.main()
        return output.getvalue()

    def test_cli_six_run_plan_and_legacy_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "design.json"
            output = self.invoke("plan", "--arms", "2,4", "--runs", "6", "--seed", "20261113",
                                 "--cards", "N01,N03,N04,D02", "--out", path)
            self.assertEqual(read_json(path), roster())
            self.assertIn("24 randomized paired blocks, 48 assignments", output)
            self.assertEqual(Counter((r["task"], r["arm"]) for r in roster()),
                             Counter({(c, a): 6 for c in ("N01", "N03", "N04", "D02") for a in (2, 4)}))
            old_plan = legacy("plan", launcher, "plan")
            self.invoke("plan", "--seed", "71", "--out", path)
            self.assertEqual(path.read_text(), json.dumps(old_plan(71), indent=2) + "\n")
            self.assertEqual(launcher.plan(20261113, ["N01", "N03", "N04", "D02"], 6),
                             old_plan(20261113, ["N01", "N03", "N04", "D02"], 6))

    def test_single_arm_four_planning(self):
        rows = launcher.plan(42, ["N01"], runs=1, arms=(4,))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["arm"], rows[0]["block_id"]), (4, "N01-r1"))

    def test_invalid_arm_selection(self):
        for arms in ((), (2, 2, 4), (4, 2), (True, 2, 3, 4), (2.0, 4)):
            with self.subTest(arms=arms), self.assertRaisesRegex(ValueError, "unsupported"):
                launcher.plan(71, arms=arms)

    def test_cli_dispatches_complete_two_and_four_arm_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for arms in ((2, 4), (1, 2, 3, 4)):
                design = roster(arms)
                write_json(root / "design.json", design)
                with self.subTest(arms=arms), patch.object(launcher, "run_block") as run:
                    self.invoke("run", "--design", root / "design.json", "--runs-root", root / "runs", "--stub")
                    self.assertEqual(run.call_count, 24)
                    self.assertTrue(all([r["arm"] for r in call.args[0]] == list(arms)
                                        for call in run.call_args_list))
                    self.assertTrue(all(call.args[2] is True for call in run.call_args_list))

    def test_invalid_design_rejected_before_any_dispatch_even_outside_selected_block(self):
        valid = roster()
        malformed = [[], valid[:-1], valid + [valid[-1]],
                     [*valid[:-1], dict(valid[-1], arm=2)],
                     [*valid[:-1], dict(valid[-1], arm="4")],
                     [*valid[:-1], dict(valid[-1], arm=True)],
                     [*valid[:-1], dict(valid[-1], arm=4.0)],
                     [r for r in valid if r["arm"] == 4],
                     [*valid, *roster((1, 2, 3, 4))[-4:]]]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, design in enumerate(malformed):
                write_json(root / "design.json", design)
                with self.subTest(case=index), patch.object(launcher, "run_block") as run, \
                        contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    self.invoke("run", "--design", root / "design.json", "--runs-root", root / "runs",
                                "--block", valid[0]["block_id"])
                self.assertEqual(error.exception.code, 2)
                run.assert_not_called()
                self.assertFalse((root / "runs").exists())


class SelectedArmGrading(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.record = self.root / "runs/N01-r6/block_record.json"

    def block(self, arms=(2, 4), **fields):
        block = dict(block_id="N01-r6", card="N01", run_index=6, status="ok", complete=True,
                     started_at="2030-01-01T00:00:00Z",
                     run_dirs={str(a): str(self.record.parent / f"arm{a}") for a in arms})
        write_json(self.record, dict(block, **fields))

    def grade(self, **kwargs):
        return grading.grade_block(self.record, self.root / "grades", self.root / "results.json", **kwargs)

    @staticmethod
    def outcome(run, out, reason, identity):
        return dict(identity, family="breast_cancer", task="N01", run=6,
                    status="incident" if reason else "valid", reason=reason,
                    verified_success=False, card_success=False)

    def test_infers_recorded_arms_and_keeps_arm4_development_path(self):
        for arms in ((2, 4), (1, 2, 3, 4), (4,)):
            self.block(arms)
            with self.subTest(arms=arms), patch.object(grading, "grade_run", side_effect=self.outcome) as grade:
                self.assertEqual([r["arm"] for r in self.grade()], list(arms))
                self.assertEqual(grade.call_count, len(arms))
                self.assertEqual([call.args[0].name for call in grade.call_args_list], [f"arm{a}" for a in arms])
                self.assertEqual(self.grade(arms=arms), read_json(self.root / "results.json")[-len(arms):])
            self.block(arms, complete=False)
            with patch.object(grading, "grade_run") as grade, self.assertRaisesRegex(ValueError, "whole block"):
                self.grade()
            grade.assert_not_called()

    def test_explicit_arm_subset_duplicate_or_wrong_type_rejected_before_grading(self):
        for recorded in ((2, 4), (1, 2, 3, 4), (4,)):
            self.block(recorded)
            invalid = [(), (2,), (2, 2, 4), (2, 4, 1), (2.0, 4), (True, 2, 3, 4)]
            if recorded != (4,):
                invalid.append((4,))
            for arms in invalid:
                with self.subTest(recorded=recorded, arms=arms), patch.object(grading, "grade_run") as grade, \
                        self.assertRaisesRegex(ValueError, "complete recorded"):
                    self.grade(arms=arms)
                grade.assert_not_called()
        for recorded in ((), (2,), (1, 4), (1, 2, 4), ("02", "4"), ("5",)):
            self.block(recorded)
            with self.subTest(recorded=recorded), patch.object(grading, "grade_run") as grade, \
                    self.assertRaises(ValueError):
                self.grade()
            grade.assert_not_called()

    def test_grader_incident_invalidates_whole_pair_and_persists_it(self):
        self.block()
        def grade(run, out, reason, identity):
            result = self.outcome(run, out, reason, identity)
            if identity["arm"] == 4:
                result.update(status="incident", reason="fabricated grader incident")
            return result
        with patch.object(grading, "grade_run", side_effect=grade):
            records = self.grade()
        self.assertTrue(all(r["status"] == "incident" and not r["verified_success"] for r in records))
        self.assertEqual(read_json(self.record)["status"], "incident")
        self.assertEqual(read_json(self.root / "results.json"), records)
        for arm in (2, 4):
            self.assertEqual(read_json(self.root / f"grades/N01-r6/arm{arm}/grade.json")["reason"],
                             "fabricated grader incident")

    def test_existing_incident_is_passed_to_every_selected_grader(self):
        self.block(status="incident", incident_reason="fabricated provider incident")
        with patch.object(grading, "grade_run", side_effect=self.outcome) as grade:
            self.assertTrue(all(row["status"] == "incident" for row in self.grade()))
            self.assertEqual([call.args[2] for call in grade.call_args_list],
                             ["fabricated provider incident"] * 2)


class SelectedArmAnalysis(unittest.TestCase):
    def test_estimator_primary_and_every_bootstrap_draw_equal_four_arm_oracle(self):
        old = legacy("estimator", estimator, "analyze")
        # Unequal family/task/run counts exercise all three balancing/resampling levels.
        four = [dict(family=f, task=t, run=r, arm=a, primary_eligible=True)
                for f, tasks in (("large", ("a", "b")), ("small", ("c",)))
                for t in tasks for r in range(1, {"a": 3, "b": 5, "c": 4}[t] + 1)
                for a in (1, 2, 3, 4)]
        two = [r for r in four if r["arm"] in (2, 4)]
        quantile = np.quantile
        for seed in (71, 20261113):
            with self.subTest(seed=seed), patch.object(estimator.np, "quantile", wraps=quantile) as percentile:
                reference = old(four, outcomes(four), bootstrap=257, seed=seed)
                historical = estimator.analyze(four, outcomes(four), bootstrap=257, seed=seed)
                selected = estimator.analyze(two, outcomes(two), bootstrap=257, seed=seed)
            self.assertEqual(json.dumps(historical, indent=2), json.dumps(reference, indent=2))
            self.assertEqual(selected["arms"], [2, 4])
            self.assertEqual(selected["assigned_primary_runs"], len(two))
            for key in ("primary_4_minus_2", "primary_95_percentile_interval"):
                self.assertEqual(selected[key], reference[key])
            draws = [call.args[0] for call in percentile.call_args_list]
            np.testing.assert_array_equal(draws[0], draws[1])
            np.testing.assert_array_equal(draws[0], draws[2])

    def test_estimator_rejects_unpaired_design_and_retains_missing_outcome_semantics(self):
        design = roster()
        for bad in (design[:-1], design + [design[-1]],
                    [r for r in design if r["arm"] == 4]):
            with self.subTest(size=len(bad)), self.assertRaises(ValueError):
                estimator.analyze(bad, outcomes(bad), bootstrap=10)
        result = estimator.analyze(design, outcomes(design)[:-1], bootstrap=10)
        self.assertEqual(result["assigned_primary_runs"], 48)
        self.assertEqual(result["missing_results_scored_zero"], 1)
        self.assertEqual(result["unknown_billed_cost_entries"], 48)

    def test_two_arm_tables_figures_and_actual_estimator_subprocess(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"EIDOS_RESEARCH_PYTHON": sys.executable}):
            root = Path(directory)
            write_json(root / "design.json", roster())
            write_json(root / "results.json", outcomes(roster()))
            descriptive.analyze(root / "design.json", root / "results.json", root / "analysis")
            report = read_json(root / "analysis/descriptive.json")
            for table in report.values():
                self.assertEqual({r["arm"] for r in table}, {2, 4})
            self.assertEqual(len(report["successes"]), 8)
            self.assertTrue(all(r["n"] == 6 for r in report["successes"]))
            self.assertEqual(read_json(root / "analysis/estimator.json")["arms"], [2, 4])
            for name in ("success_by_arm_card.png", "resources_by_arm.png"):
                self.assertEqual((root / "analysis" / name).read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_existing_pilot3_fixture_estimator_tables_and_pngs_unchanged(self):
        design, results = read_json(HISTORY / "plan.json"), read_json(HISTORY / "dry-run-results.json")
        # The retained dry run covers two blocks of the larger planned census.
        captured = {(r["task"], r["run"]) for r in results}
        design = [r for r in design if (r["task"], r["run"]) in captured]
        result = estimator.analyze(design, results)
        self.assertEqual(result, read_json(HISTORY / "dry-run-estimator.json"))
        old_estimator = legacy("estimator", estimator, "analyze")
        self.assertEqual(json.dumps(result, indent=2), json.dumps(old_estimator(design, results), indent=2))
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"EIDOS_RESEARCH_PYTHON": sys.executable}), \
                patch.object(descriptive, "audit", return_value={"offline": "committed fixture only"}):
            root = Path(directory)
            write_json(root / "design.json", design)
            old_analysis = legacy("descriptive", descriptive, "analyze")
            # Never dereference the fixture's external transcript paths.
            old_analysis(root / "design.json", HISTORY / "dry-run-results.json", root / "old")
            descriptive.analyze(root / "design.json", HISTORY / "dry-run-results.json", root / "new")
            names = ("estimator.json", "descriptive.json", "transcript_audit.json", "successes.csv",
                     "statuses.csv", "resources.csv", "report.md", "success_by_arm_card.png", "resources_by_arm.png")
            for name in names:
                with self.subTest(file=name):
                    self.assertEqual((root / "old" / name).read_bytes(), (root / "new" / name).read_bytes())

    def test_historical_charter_bytes_unchanged_and_pilot4_guidance_mentions_selected_arms(self):
        old = legacy("charter", charters, "charter")
        for card in ("D01", "D02", "D03", "D04", "D05", "D06", "D12", "N01", "N02", "N03", "N04"):
            with self.subTest(card=card):
                self.assertEqual(json.dumps(charters.charter(card)), json.dumps(old(card)))
        for card in ("N05", "N06", "N07", "N08"):
            setup = charters.charter(card)["pilot_2_setup"]
            self.assertIn("coding-agent research interpreter", setup["environments"])
            self.assertIn("Arm 2 receives METHOD.md", setup["information"])
            self.assertNotIn("Arm 3", setup["information"])


if __name__ == "__main__":
    unittest.main()
