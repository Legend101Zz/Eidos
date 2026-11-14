"""Controller-only verification and staging of a private prospective card seal."""

import hashlib
import io
import json
import math
import os
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from study.private_card_registry import PRIVATE_CARD_BARS, PROFILES, card_bars, profile_for_card, validate_bars

MODEL_FILES = frozenset({
    "brief.md", "project.py", "train.npz", "validation.npz",
    "requirements.lock", "task.json", "answer_schema.py",
})
GRADER_FILES = frozenset({"test_inputs.npz", "test_labels.npz", "baseline_test.npz"})
CARD_FILES = frozenset(f"bundle/{name}" for name in MODEL_FILES) | GRADER_FILES


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _sealed_file(root, name):
    relative = PurePosixPath(name)
    if name != relative.as_posix() or relative.is_absolute() or any(
        part == ".." for part in relative.parts
    ):
        raise ValueError("invalid sealed file path")
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("symlink in private seal")
    if not path.is_file():
        raise ValueError("missing private seal file")
    return path


class PrivateSeal:
    def __init__(self, root, commitment, cards=None):
        if cards is not None and not isinstance(cards, Mapping):
            cards = {card: card_bars(card) for card in cards}
        self.cards = validate_bars(PRIVATE_CARD_BARS if cards is None else cards,
                                  PRIVATE_CARD_BARS if cards is None else cards)
        self.root = Path(root).resolve(strict=True)
        opening = json.loads(_sealed_file(self.root, "commitment-opening.json").read_bytes())
        hashes = opening["files"]
        salt = bytes.fromhex(opening["salt"])
        canonical = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
        if len(salt) != 32 or _sha(salt + canonical) != commitment or opening["sha256"] != commitment:
            raise ValueError("private seal commitment mismatch")
        required = {
            f"{card}/{name}"
            for card in self.cards
            for name in CARD_FILES | {"manifest.json"}
        }
        if not required <= hashes.keys():
            raise ValueError("private seal opening omits required files")
        for name, expected in hashes.items():
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError("invalid private seal digest")
            if _sha(_sealed_file(self.root, name).read_bytes()) != expected:
                raise ValueError("private seal file digest mismatch")
        self.hashes = hashes

    def card(self, card):
        metric, bar = self.cards[card]
        manifest = json.loads(self._read(card, "manifest.json"))
        kind = "classification" if metric == "accuracy" else "regression"
        seeds = manifest["seeds"]
        threshold = manifest["min_reference_gain"]
        if (
            len(seeds) != 3 or any(type(seed) is not int for seed in seeds) or len(set(seeds)) != 3
            or manifest["id"] != card or manifest["task_type"] != kind
            or manifest["metric"] != metric
            or type(threshold) not in (int, float) or not math.isfinite(threshold) or threshold <= 0
            or (metric == "accuracy" and threshold != bar)
            or manifest["qualified"] is not True
            or manifest["files"] != {name: self.hashes[f"{card}/{name}"] for name in CARD_FILES}
        ):
            raise ValueError("private card manifest mismatch")
        return manifest

    def _read(self, card, name):
        if card not in self.cards:
            raise ValueError("card outside configured private profile")
        relative = f"{card}/{name}"
        data = _sealed_file(self.root, relative).read_bytes()
        if _sha(data) != self.hashes[relative]:
            raise ValueError("private seal file digest changed")
        return data

    def stage_bundle(self, card, destination):
        self.card(card)
        files = {name: self._read(card, f"bundle/{name}") for name in MODEL_FILES}
        task = json.loads(files["task.json"])
        if task["task_id"] != card or task["project"] != card:
            raise ValueError("private task identity mismatch")
        if task["hashes"] != {
            name: _sha(data) for name, data in files.items() if name != "task.json"
        }:
            raise ValueError("private task hash inventory mismatch")
        destination = Path(destination)
        destination.mkdir()
        for name, data in files.items():
            (destination / name).write_bytes(data)

    def stage_test_inputs(self, card, destination):
        import numpy as np

        data = self._read(card, "test_inputs.npz")
        with np.load(io.BytesIO(data), allow_pickle=False) as inputs:
            if set(inputs.files) != {"x", "ids"}:
                raise ValueError("invalid private test input schema")
        Path(destination).write_bytes(data)

    def score(self, card, candidate):
        import sys

        import numpy as np

        from study.common import DEVELOPMENT

        sys.path.insert(0, str(DEVELOPMENT))
        try:
            from grade import read_predictions, score
        finally:
            sys.path.pop(0)

        manifest = self.card(card)
        kind = manifest["task_type"]
        classes = manifest.get("nclasses", 2) if kind == "classification" else None
        if kind == "classification" and (type(classes) is not int or classes < 2):
            raise RuntimeError("private manifest class count invalid")
        try:
            with np.load(io.BytesIO(self._read(card, "test_labels.npz")), allow_pickle=False) as labels:
                if set(labels.files) != {"y", "ids"}:
                    raise ValueError("invalid private label schema")
                y, ids = labels["y"], labels["ids"]
            baseline_file = io.BytesIO(self._read(card, "baseline_test.npz"))
            baseline = score(y, read_predictions(baseline_file, ids, kind, classes), kind)
        except (KeyError, TypeError, ValueError, OSError) as error:
            raise RuntimeError("private scorer evidence failed verification") from error
        measured = score(y, read_predictions(candidate, ids, kind, classes), kind)
        if kind == "regression" and set(self.cards) != set(PRIVATE_CARD_BARS) and not math.isclose(
            manifest["min_reference_gain"], self.cards[card][1] * baseline,
            rel_tol=1e-12, abs_tol=0.0,
        ):
            raise RuntimeError("private RMSE relative bar failed verification")
        return dict(metric=manifest["metric"], baseline=baseline, candidate=measured,
                    gain=measured - baseline if kind == "classification" else baseline - measured,
                    n=len(y), split="test", status="numeric development result")


def configured_seal(card=None, *, profile=None):
    try:
        selected = profile or os.environ.get("EIDOS_CARD_PROFILE")
        if card is not None:
            expected = profile_for_card(card)
            if selected is not None and selected != expected:
                raise ValueError("card/profile mismatch")
            selected = expected
        spec = PROFILES[selected or "pilot-4"]
        commitment = spec.commitment()
        cards = spec.bars()
        return PrivateSeal(spec.seal_root(), commitment, cards)
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise RuntimeError("configured private seal failed verification") from error
