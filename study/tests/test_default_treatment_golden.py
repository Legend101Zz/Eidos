"""Pilot 4 goldens, computed from origin/main (806f939), never branch expectations.

The archive command and hashing recipe are recorded in WP2_REPORT.md. The archive
contained only public source paths; N05 rendering uses public metadata, no seal.
"""
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from study.common import render_prompt
from study.exp import flags
from study.exp.treatment import for_run, merge_patch, resolve
from study.toolkit.build_toolkit import build_toolkit

ROOT = Path(__file__).resolve().parents[2]
# git show origin/main:<path> | shasum -a 256
INVESTIGATOR_SHA = "aacc14a0fa2419d347a1e58c31e765d37ac3b9c941834ab80ae0fe74e2aaaaea"
TOOLS_SHA = "2321e7f9a34e52503030404437fa8c12aa72c87f1f8cc54f10bef0d98ba1deed"
# Main's build_toolkit, imported from a git archive in a subprocess. Hash sorted
# relative POSIX path + NUL + file bytes + NUL, including manifest.json.
TOOLKIT_SHA = "f9dad33df6e50987db549880396fd6486554eabb28226a4859c86681fcc665d4"
# hashlib.sha256(main.study.common.render_prompt(4, card).encode()).hexdigest()
PROMPTS = {"N01": "6b7cb8f6b00fee96020f504b4594bb763dc62855c72927867482cdbedfc36be8",
           "N05": "5fae5056d597310b521c67ee29d053b789b1754ad1c6993c7f61b19c76d2d1e6"}
# sha256(bundle_dir(archive/components/<name>)), using main's unchanged CLI
# encoding (sorted JSON files, base64 binary fixtures).
BUNDLES = [
    ("eidos.project.command@1.0.9", "d6ea9fdd53b1936f17ef8c65f21be2d7928e219dfa2707822118d09ff15fa6a7", True),
    ("eidos.diagnose.baseline-errors@1.0.0", "82548802d9c50619dd4ce7e0b8418673044f4809fab17272230c8916773a281c", True),
    ("eidos.search.recipe-sweep@1.1.0", "22068c870b4463189e8dfde0266a6b695b29858304a71f456d842a870e023e07", True),
    ("eidos.search.recipe-sweep@1.0.0", "8cf11f80f69a054e8500a26ef3bc26456bbc990ea024531481b84a9110c13623", False),
    ("eidos.task.paired-classification-report@1.0.0", "44535b1939bd78fd7bed82b1954ad494b23c62e841dd4810c74b7007274b5298", True),
    ("eidos.task.score-classification@1.0.2", "9ca030f707340e3521fba9f096cc3cfb7ea57a4e5a699594f09a14da50f0e8ae", True),
]
# git show origin/task/v2-step3:study/eidos_arms/investigator.json (parsed).
STEP3 = {"runtime": "pydantic-ai", "model": "openai-compatible:MiniMax-M3.1-Flash", "max_turns": 10000,
         "max_calls_per_turn": 32, "max_model_calls_per_turn": 32, "turn_deadline_seconds": 1800,
         "settings": {"base_url": "https://api.minimax.io/v1", "output_mode": "json", "max_output_tokens": 16384,
                      "request_timeout": 300, "max_requests_per_decision": 8, "max_tokens_per_decision": 262144},
         "context_policy": "context-policy/4", "exploration": {"policy": "exploration-policy/3",
         "project_command_ref": "eidos.project.command@1.0.9", "selection_margin_multiple": 1.0}}


def sha(data):
    return hashlib.sha256(data).hexdigest()


class DefaultTreatmentGolden(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"EIDOS_STUDY_TREATMENT": "pilot4-v1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_base_investigator_is_main_bytes(self):
        data = (ROOT / "study/eidos_arms/investigator.json").read_bytes()
        self.assertEqual(sha(data), INVESTIGATOR_SHA)
        self.assertEqual(resolve().investigator, json.loads(data))

    def test_main_tool_prompt_and_full_public_and_pilot4_prompts(self):
        self.assertEqual(sha((ROOT / "study/prompts/tools_arm4.md").read_bytes()), TOOLS_SHA)
        for card, expected in PROMPTS.items():
            self.assertEqual(sha(render_prompt(4, card).encode()), expected)
        with patch.dict(os.environ, EIDOS_STUDY_TREATMENT="v2-portfolio"):
            self.assertNotEqual(sha(render_prompt(4, "N01").encode()), PROMPTS["N01"])

    def test_exact_component_refs_bundle_bytes_order_and_bindings(self):
        # Keep this runnable in the stdlib-only research interpreter as well.
        import base64
        def bundle(directory):
            files = {}
            for p in sorted(directory.rglob("*")):
                if p.is_file() and not p.is_symlink() and "__pycache__" not in p.parts and not p.name.endswith(".pyc"):
                    data = p.read_bytes()
                    try:
                        files[str(p.relative_to(directory))] = data.decode()
                    except UnicodeDecodeError:
                        files[str(p.relative_to(directory))] = {"base64": base64.b64encode(data).decode()}
            return json.dumps({"files": files}, sort_keys=True).encode()
        for card in ("N01", "N02", "D03"):
            expected = [entry for entry in BUNDLES if card != "N02" or not entry[0].startswith("eidos.task.paired-")]
            actual = []
            for directory, bind in resolve().component_plan(card):
                manifest = json.loads((directory / "eidos-component.json").read_text())
                actual.append((manifest["id"] + "@" + manifest["release"], sha(bundle(directory)), bind))
            self.assertEqual(actual, expected, card)

    def test_full_toolkit_tree_equals_main(self):
        with tempfile.TemporaryDirectory() as scratch:
            toolkit = build_toolkit(scratch)
            h = hashlib.sha256()
            for p in sorted(toolkit.rglob("*")):
                if p.is_file():
                    h.update(p.relative_to(toolkit).as_posix().encode())
                    h.update(b"\0")
                    h.update(p.read_bytes())
                    h.update(b"\0")
            self.assertEqual(h.hexdigest(), TOOLKIT_SHA)
            with self.assertRaisesRegex(ValueError, "differs"):
                build_toolkit(scratch, treatment=resolve("v2-portfolio"))

    def test_v2_reproduces_step3_investigator(self):
        self.assertEqual(resolve("v2-portfolio").investigator, STEP3)
        self.assertEqual(resolve("v2-portfolio").tools_arm4_path.name, "tools_arm4.v2.md")

    def test_merge_patch_rfc7396(self):
        base = {"a": {"b": 1, "c": [2]}, "d": 3}
        self.assertEqual(merge_patch(base, {"a": {"b": None, "c": [4]}, "d": None}), {"a": {"c": [4]}})
        self.assertEqual(base, {"a": {"b": 1, "c": [2]}, "d": 3})
        self.assertEqual(merge_patch(base, [1]), [1])
        self.assertEqual(merge_patch(2, {"x": 3}), {"x": 3})

    def test_unknown_treatment_and_flag_refuse(self):
        with self.assertRaisesRegex(ValueError, "unknown treatment"):
            resolve("typo")
        with patch.dict(os.environ, EIDOS_STUDY_TREATMENT="typo"), self.assertRaisesRegex(ValueError, "unknown treatment"):
            resolve()
        definition = json.loads((flags.TREATMENTS / "v2-portfolio.json").read_text())
        definition["flags"] = ["typo"]
        registry = flags.load_registry()
        with (patch("study.exp.treatment.json.loads", return_value=definition),
              patch.object(flags, "load_registry", return_value=registry), self.assertRaisesRegex(ValueError, "unknown flag")):
            resolve("v2-portfolio")

    def test_rejection_replay_removal_list_and_default_rules(self):
        original_sha = resolve("v2-portfolio").sha256
        registry = flags.load_registry()
        flag = registry["flags"]["floor.selection-margin"]
        flag.update(state="rejected", removal=dict(decided="2026-11-14", experiment="EXP-test", reason="no benefit"))
        flags.validate_registry(registry)
        with patch.object(flags, "load_registry", return_value=registry):
            with self.assertRaisesRegex(ValueError, "rejected flag"):
                resolve("v2-portfolio")
            replay = resolve("v2-portfolio", allow_rejected=True)
            self.assertEqual(replay.id, "v2-portfolio")
            self.assertEqual(replay.sha256, original_sha)
            removals = flags.removal_list()
            self.assertEqual(removals[0]["id"], "floor.selection-margin")
            self.assertEqual(removals[0]["treatments"], ["v2-portfolio"])
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(flags.main(["removal-list"]), 0)
            self.assertEqual(json.loads(output.getvalue()), removals)
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "pilot4-v1.json"
            definition = dict(schema="eidos-treatment/1", id="pilot4-v1", summary="test", flags=["diagnose.steering-fix"], overrides={})
            path.write_text(json.dumps(definition))
            with patch.object(flags, "TREATMENTS", Path(scratch)), self.assertRaisesRegex(ValueError, "default treatment"):
                resolve("pilot4-v1")

    def test_registry_validation_and_dependencies(self):
        for change, message in (({"state": "bad"}, "lifecycle"), ({"requires": ["typo"]}, "unknown required"),
                                ({"removal": {}}, "only present"), ({"code": ["missing-file"]}, "missing code"),
                                ({"patch": {"prompts": {"tools_arm4": "missing-file"}}}, "missing prompt"),
                                ({"requires": ["floor.selection-margin"]}, "cyclic")):
            registry = flags.load_registry()
            registry["flags"]["floor.selection-margin"].update(change)
            with self.assertRaisesRegex(ValueError, message):
                flags.validate_registry(registry)
        with tempfile.TemporaryDirectory() as scratch:
            definition = dict(schema="eidos-treatment/1", id="missing-dependency", summary="test", flags=["floor.selection-margin"], overrides={})
            (Path(scratch) / "missing-dependency.json").write_text(json.dumps(definition))
            with patch.object(flags, "TREATMENTS", Path(scratch)), self.assertRaisesRegex(ValueError, "lacks required"):
                resolve("missing-dependency")

    def test_treatment_receipt_refuses_worker_drift(self):
        meta = {"treatment": resolve().record()}
        self.assertEqual(for_run(meta).id, "pilot4-v1")
        with patch.dict(os.environ, EIDOS_STUDY_TREATMENT="v2-portfolio"), self.assertRaisesRegex(ValueError, "differs"):
            for_run(meta)

    def test_flag_null_deletion_then_override_and_adopted_alias(self):
        registry = flags.load_registry()
        registry["flags"]["floor.v2-slice"]["patch"]["investigator"]["settings"] = {"request_timeout": None}
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch)
            definition = dict(schema="eidos-treatment/1", id="merge-test", summary="test",
                              flags=["floor.v2-slice"], overrides={"settings": {"max_output_tokens": 32}})
            (path / "merge-test.json").write_text(json.dumps(definition))
            with patch.object(flags, "TREATMENTS", path), patch.object(flags, "load_registry", return_value=registry):
                treatment = resolve("merge-test")
                self.assertNotIn("request_timeout", treatment.investigator["settings"])
                self.assertEqual(treatment.investigator["settings"]["max_output_tokens"], 32)
            alias = registry["flags"]["diagnose.steering-fix"]
            alias.update(state="adopted", patch={})
            flags.validate_registry(registry)
            definition.update(id="pilot4-v1", flags=["diagnose.steering-fix"], overrides={})
            (path / "pilot4-v1.json").write_text(json.dumps(definition))
            with patch.object(flags, "TREATMENTS", path), patch.object(flags, "load_registry", return_value=registry):
                with self.assertRaisesRegex(ValueError, "default treatment"):
                    resolve()

    def test_registry_cli_check(self):
        result = subprocess.run([sys.executable, "-m", "study.exp.flags", "check"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
