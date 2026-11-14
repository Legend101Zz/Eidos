"""Profile controls use only fabricated seals and explicitly synthetic metadata."""

import io
import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np

from study import common, grade_block, launcher
from study.eidos_arms import charters, lab
from study.private_card_registry import PROFILES, SEAL_ROOT_ENVS, CardProfile
from study.private_seal import MODEL_FILES, PrivateSeal, configured_seal
from study.reconcile_seal import verify_profile
from study.tests.test_private_seal import make_seal

# Generated once from d83abaca48c18617e14acd1d540a10cc7532993b via
# git show BASE:<path> and exec of common, charters, launcher, grade_block and
# private_seal. SHA-256 of render_prompt(...).encode(), json.dumps(charter(...))
# and json.dumps(plan(42, ...)); grade_run uses make_seal + stub on N05-N08,
# a fixed run_record wall_s=0, and replaces the temporary root with "<TMP>" in
# json.dumps(result). No history or historical code is loaded at test time.
GOLDENS = json.loads((Path(__file__).parent / "fixtures/card_profile_goldens.json").read_text())


def fabricated_bars(profile):
    # This assignment is a test fixture, NOT metadata for the real cards.
    return {card: ("accuracy", .02) if index % 2 == 0 else ("rmse", .05)
            for index, card in enumerate(PROFILES[profile].cards)}


def byte_proof(test, name, after):
    """Check every output byte against the committed historical digest."""
    output = os.environ.get("EIDOS_PROFILE_BYTE_PROOF_DIR")
    if output:
        root = Path(output)
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{name}.after").write_bytes(after)
        (root / f"{name}.expected-sha256").write_text(GOLDENS[name] + "\n")
    test.assertEqual(hashlib.sha256(after).hexdigest(), GOLDENS[name], name)


class Profiles(unittest.TestCase):
    def test_published_commitments_load_but_unpublished_metadata_refuses(self):
        with patch.dict(os.environ, {}, clear=True):
            for name in ("pilot-5", "pilot-6"):
                with self.assertRaisesRegex(ValueError, "unknown.*trusted plane"):
                    PROFILES[name].bars()
                for card in PROFILES[name].cards:
                    with self.assertRaisesRegex(ValueError, "unknown"):
                        common.render_prompt(2, card)
                    with self.assertRaisesRegex(ValueError, "unknown"):
                        charters.charter(card)
                with self.assertRaisesRegex(ValueError, "unknown"):
                    launcher.plan(42, profile=name)
            public = common.STUDY.joinpath("prospective_commitments.txt").read_text().splitlines()[2].split()[-1]
            self.assertEqual(PROFILES["pilot-6"].commitment(), public)
            with self.assertRaisesRegex(RuntimeError, "failed verification"):
                configured_seal(profile="pilot-6")
        published = common.STUDY.joinpath("prospective_commitments.txt").read_text().splitlines()[1].split()[-1]
        self.assertEqual(PROFILES["pilot-5"].commitment(), published)

    def test_invalid_or_partial_metadata_and_mismatched_selection_refuse(self):
        for metadata in ({"N09": ["accuracy", .02]},
                         {card: ["accuracy", .05] for card in PROFILES["pilot-5"].cards},
                         {card: ["mystery", .02] for card in PROFILES["pilot-5"].cards}):
            with patch.dict(os.environ, EIDOS_CARD_PROFILE_METADATA=json.dumps(metadata)):
                with self.assertRaises(ValueError):
                    PROFILES["pilot-5"].bars()
        with self.assertRaisesRegex(RuntimeError, "failed verification"):
            configured_seal("N09", profile="pilot-4")
        with self.assertRaisesRegex(ValueError, "mismatch"):
            launcher.plan(42, ["N05"], profile="pilot-5")

    def test_fabricated_seals_stage_score_and_verify_all_eight_cards(self):
        for name in ("pilot-5", "pilot-6"):
            bars = fabricated_bars(name)
            with self.subTest(profile=name), TemporaryDirectory() as directory:
                root = Path(directory)
                digest = make_seal(root / "seal", bars)
                seal = PrivateSeal(root / "seal", digest, bars)
                with (patch.dict(os.environ, {PROFILES[name].seal_root_env: str(root / "seal"),
                                              "EIDOS_CARD_PROFILE_METADATA": json.dumps(bars)}),
                      patch.object(CardProfile, "commitment", return_value=digest)):
                    self.assertEqual(set(configured_seal(profile=name).cards), set(bars))
                    self.assertEqual(PrivateSeal(root / "seal", digest, PROFILES[name].cards).cards, bars)
                    self.assertEqual(verify_profile(name), dict(verified=True, cards=4, relative_bars_verified=True))
                    self.assertEqual(len(launcher.plan(42, profile=name, arms=(2, 4))), 24)
                    for card, (metric, _) in bars.items():
                        seal.stage_bundle(card, root / card)
                        self.assertEqual({p.name for p in (root / card).iterdir()}, MODEL_FILES)
                        predictions = root / "predictions.npz"
                        np.savez(predictions, ids=[20, 10], predictions=[1, 0])
                        score = seal.score(card, predictions)
                        self.assertGreater(score["gain"], seal.card(card)["min_reference_gain"])
                        self.assertEqual(score["candidate"], 1.0 if metric == "accuracy" else 0.0)
                        for arm in (1, 2, 3, 4):
                            self.assertIn("5%" if metric == "rmse" else "0.02", common.render_prompt(arm, card))
                        self.assertNotIn("Pilot 4 charter", json.dumps(charters.charter(card)))
                with self.assertRaisesRegex(ValueError, "omits required"):
                    PrivateSeal(root / "seal", digest)
                with self.assertRaisesRegex(ValueError, "outside configured"):
                    seal.stage_test_inputs("N05", root / "wrong-profile.npz")
                with self.assertRaisesRegex(ValueError, "commitment"):
                    PrivateSeal(root / "seal", "0" * 64, bars)
                target = root / "seal" / next(iter(bars)) / "bundle/brief.md"
                target.write_text("tampered synthetic data")
                with self.assertRaisesRegex(ValueError, "digest changed"):
                    seal.stage_bundle(next(iter(bars)), root / "tampered")
                with self.assertRaisesRegex(ValueError, "digest mismatch"):
                    PrivateSeal(root / "seal", digest, bars)
                target.unlink()
                target.symlink_to(root / next(iter(bars)) / "brief.md")
                with self.assertRaisesRegex(ValueError, "symlink"):
                    PrivateSeal(root / "seal", digest, bars)

    def test_rmse_bar_must_match_the_sealed_baseline(self):
        bars = fabricated_bars("pilot-6")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            digest = make_seal(root / "seal", bars)
            seal = PrivateSeal(root / "seal", digest, bars)
            card = PROFILES["pilot-6"].cards[1]
            manifest = seal.card(card)
            manifest["min_reference_gain"] *= 2
            with patch.object(seal, "card", return_value=manifest):
                with self.assertRaisesRegex(RuntimeError, "relative bar"):
                    seal.score(card, io.BytesIO(seal._read(card, "baseline_test.npz")))

    def test_manifest_class_count_defaults_to_two_and_supports_multiclass(self):
        bars = fabricated_bars("pilot-5")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            digest = make_seal(root / "seal", bars)
            seal = PrivateSeal(root / "seal", digest, bars)
            card = PROFILES["pilot-5"].cards[0]
            predictions = root / "predictions.npz"
            np.savez(predictions, ids=[20, 10], predictions=[2, 0])
            manifest = seal.card(card)
            with self.assertRaises(ValueError):
                seal.score(card, predictions)
            with patch.object(seal, "card", return_value={**manifest, "nclasses": 3}):
                self.assertEqual(seal.score(card, predictions)["candidate"], .5)
            for invalid in (None, 1, True, 2.5):
                with patch.object(seal, "card", return_value={**manifest, "nclasses": invalid}):
                    with self.assertRaisesRegex(RuntimeError, "class count"):
                        seal.score(card, predictions)

    def test_all_profile_roots_are_removed_from_lab_workers(self):
        with patch.dict(os.environ, {name: "synthetic-private-root" for name in SEAL_ROOT_ENVS}):
            self.assertTrue(all(name not in lab.environment() for name in SEAL_ROOT_ENVS))

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS Seatbelt")
    def test_new_profiles_run_all_four_stub_arms_without_lab_or_models(self):
        for name in ("pilot-5", "pilot-6"):
            bars = fabricated_bars(name)
            with self.subTest(profile=name), TemporaryDirectory() as directory:
                root = Path(directory)
                digest = make_seal(root / "seal", bars)
                card = PROFILES[name].cards[0]
                python = root / "stub-build/bin/python"
                python.parent.mkdir(parents=True)
                python.symlink_to(sys.executable)
                (python.parent.parent / "pilot-build.json").write_text(json.dumps({
                    "eidos_commit_sha": "synthetic", "python": str(python),
                }))
                env = {PROFILES[name].seal_root_env: str(root / "seal"),
                       "EIDOS_STUDY_PYTHON": str(python),
                       "EIDOS_CARD_PROFILE_METADATA": json.dumps(bars),
                       "EIDOS_RESEARCH_PYTHON": sys.executable,
                       "EIDOS_PILOT_CHARTER_COMMIT": "synthetic",
                       "EIDOS_PILOT_CHARTER_PATH": "docs/experiments/pilot-4/CHARTER.md"}
                popen = subprocess.Popen
                workers = []

                def check_worker_environment(command, **kwargs):
                    if any(str(part).endswith("/stub_agent.py") for part in command):
                        self.assertTrue(all(key not in kwargs["env"] for key in SEAL_ROOT_ENVS))
                        workers.append(command)
                    return popen(command, **kwargs)

                with (patch.dict(os.environ, env), patch.object(CardProfile, "commitment", return_value=digest),
                      patch("study.launcher.subprocess.Popen", side_effect=check_worker_environment)):
                    block = launcher.run_block(launcher.plan(42, [card], runs=1), root / "blocks", stub=True)
                    self.assertEqual(block["status"], "ok", block.get("incident_reason"))
                    self.assertEqual(len(workers), 4)
                    results = grade_block.grade_block(root / "blocks" / block["block_id"] / "block_record.json",
                                                     root / "grades", root / "results.json")
                    self.assertEqual(len(results), 4)
                    self.assertTrue(all(result["status"] == "valid" and result["numeric"]["gain"] == 0
                                        for result in results), results)


class Pilot4ByteIdentity(unittest.TestCase):
    def test_prompts_charters_and_plans_match_the_requested_base_bytes(self):
        for card in PROFILES["pilot-4"].cards:
            for arm in (1, 2, 3, 4):
                byte_proof(self, f"prompt-{card}-arm{arm}", common.render_prompt(arm, card).encode())
            byte_proof(self, f"charter-{card}", json.dumps(charters.charter(card)).encode())
        for cards in (None, PROFILES["pilot-4"].cards):
            for arms in ((1, 2, 3, 4), (2, 4)):
                byte_proof(self, f"plan-{'default' if cards is None else 'pilot4'}-{'-'.join(map(str, arms))}",
                           json.dumps(launcher.plan(42, cards, arms=arms)).encode())

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS Seatbelt")
    def test_grading_matches_base_bytes_on_all_four_pilot4_cards(self):
        from study.stub_agent import stub

        with TemporaryDirectory() as directory:
            root = Path(directory)
            digest = make_seal(root / "seal")
            seal = PrivateSeal(root / "seal", digest)
            env = {"EIDOS_RESEARCH_PYTHON": sys.executable, "EIDOS_PILOT_CHARTER_COMMIT": "synthetic",
                   "EIDOS_PILOT_CHARTER_PATH": "docs/experiments/pilot-4/CHARTER.md"}
            with patch.dict(os.environ, env), patch("study.private_seal.configured_seal", return_value=seal):
                for card in PROFILES["pilot-4"].cards:
                    row = launcher.plan(42, [card], runs=1)[0]
                    run = root / card / "arm1"
                    launcher.prepare(row, run)
                    stub(run)
                    record = common.read_json(run / "run_record.json")
                    record["wall_s"] = 0
                    common.write_json(run / "run_record.json", record)
                    with patch.object(grade_block, "configured_seal", return_value=seal):
                        current = grade_block.grade_run(run, root / "grade")
                    self.assertEqual(current["status"], "valid", current)
                    byte_proof(self, f"grade-{card}",
                               json.dumps(current).encode().replace(str(root).encode(), b"<TMP>"))


if __name__ == "__main__":
    unittest.main()
