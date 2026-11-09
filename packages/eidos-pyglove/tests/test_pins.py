"""Tests that need neither pyglove nor torch: the version pin and the allowed-module rules."""

import hashlib
import json

import pytest

import eidos_pyglove as ep
from eidos_pyglove import adapter


def test_package_version_and_supported_range():
    assert ep.__version__ == "0.1.0"
    assert ep.SUPPORTED_PYGLOVE == (">=0.4.5", "<0.5")


@pytest.mark.parametrize("version", ["0.4.5", "0.4.6", "0.4.10", "0.4.5.post1", "0.4.9"])
def test_supported_versions(version):
    assert ep.version_supported(version)
    assert ep.check_pyglove_version(version) == version


@pytest.mark.parametrize(
    "version",
    ["0.4.4", "0.4.0", "0.5", "0.5.0", "0.5.1", "1.0.0", "0.4.5rc1", "0.4.6.dev0", "0.4.5+local", "x", ""],
)
def test_unsupported_versions(version):
    assert not ep.version_supported(version)
    with pytest.raises(ep.UnsupportedPyGloveError, match="outside the supported range"):
        ep.check_pyglove_version(version)


def test_missing_pyglove_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(adapter, "installed_pyglove_version", lambda: None)
    with pytest.raises(ep.UnsupportedPyGloveError, match="not installed"):
        ep.check_pyglove_version()


def test_transform_revision_is_sha256_of_canonical_json():
    builder_json = {"_type": "m.f", "b": [1, 2], "a": {"y": 1.5, "x": None}}
    patches = ["widen?width=3", {"k": 1}]
    expected = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                {"builder_json": builder_json, "patches": patches, "pyglove_version": "0.4.5"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
    )
    assert ep.transform_revision(builder_json, patches, "0.4.5") == expected
    # key order and whitespace never matter; content does
    reordered = {"a": {"x": None, "y": 1.5}, "b": [1, 2], "_type": "m.f"}
    assert ep.transform_revision(reordered, patches, "0.4.5") == expected
    assert ep.transform_revision(builder_json, patches[:1], "0.4.5") != expected
    assert ep.transform_revision(builder_json, patches, "0.4.6") != expected


def test_canonical_json_refuses_non_finite_numbers():
    with pytest.raises(ep.PyGloveAdapterError):
        ep.canonical_json({"x": float("nan")})


@pytest.mark.parametrize(
    "bad",
    [
        "torch.nn",
        ["os"],
        ["os.path"],
        ["builtins"],
        ["torch"],
        ["pyglove"],
        ["pyglove.core"],
        ["pickle"],
        [],
        ["a b"],
        [3],
    ],
)
def test_allowed_modules_rejects_broad_or_malformed_entries(bad):
    with pytest.raises((ValueError, TypeError)):
        adapter._normalize_allowed(bad)


def test_allowed_modules_accepts_narrow_names():
    assert adapter._normalize_allowed(["my_models", "torch.nn", "my_models"]) == ["my_models", "torch.nn"]
    assert adapter._within("torch.nn.modules.activation.ReLU", ["torch.nn"])
    assert not adapter._within("torch.nnx.ReLU", ["torch.nn"])
    assert not adapter._within("torch.load", ["torch.nn"])
