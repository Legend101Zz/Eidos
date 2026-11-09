"""eidos-pyglove: search space, transforms, export/load and the load-time security gate.

Skipped as a whole where pyglove or torch is not installed.
"""

import json

import pytest

pg = pytest.importorskip("pyglove")
torch = pytest.importorskip("torch")

import pg_mlp_helper as helper  # noqa: E402  (test helper next to this file)

import eidos_pyglove as ep  # noqa: E402
from eidos_pyglove import adapter  # noqa: E402

ALLOWED = ["pg_mlp_helper", "torch.nn"]


@pytest.fixture()
def space():
    return ep.SearchSpace(helper.make_space())


def layer_shapes(module):
    return [tuple(p.shape) for p in module.parameters()]


def rewrite_export(path, mutate, *, rehash):
    """Edit an export file; with ``rehash`` the revision is recomputed (a hostile but consistent file)."""
    doc = json.loads(path.read_text())
    mutate(doc)
    if rehash:
        doc["transform_revision"] = ep.transform_revision(
            doc["builder_json"], doc["patches"], doc["pyglove_version"]
        )
    path.write_text(json.dumps(doc))
    return path


# -- search space ---------------------------------------------------------------------------


def test_space_size_and_deterministic_candidate_order(space):
    assert space.size == 6
    first = space.candidates()
    second = ep.SearchSpace(helper.make_space()).candidates()
    assert len(first) == 6
    assert [c.dna for c in first] == [c.dna for c in second]
    assert [c.revision()["transform_revision"] for c in first] == [
        c.revision()["transform_revision"] for c in second
    ]
    assert len({tuple(c.dna) for c in first}) == 6
    widths = [c.value.hidden for c in first]
    assert widths == [16, 16, 32, 32, 64, 64]
    assert [type(c.build()[1]).__name__ for c in first[:2]] == ["ReLU", "Tanh"]


def test_candidates_limit_takes_a_prefix_of_the_sweep(space):
    assert [c.dna for c in space.candidates(limit=2)] == [c.dna for c in space.candidates()][:2]
    assert space.candidates(limit=0) == []
    with pytest.raises(ValueError):
        space.candidates(limit=-1)


def test_materialize_dna_builds_a_module_with_the_right_output_shape(space):
    candidate = space.materialize([1, 0])
    assert candidate.dna == [1, 0]
    assert candidate.value.hidden == 32
    module = candidate.build()
    assert isinstance(module, torch.nn.Sequential)
    assert layer_shapes(module) == [(32, 8), (32,), (2, 32), (2,)]
    assert isinstance(module[1], torch.nn.ReLU)
    assert tuple(module(torch.randn(5, 8)).shape) == (5, 2)
    assert isinstance(space.materialize([2, 1]).build()[1], torch.nn.Tanh)


def test_build_returns_a_fresh_module_every_call(space):
    candidate = space.materialize([0, 0])
    a, b = candidate.build(), candidate.build()
    assert a is not b
    assert a[0].weight.data_ptr() != b[0].weight.data_ptr()


@pytest.mark.parametrize("bad", [[3, 0], [1], [1, 0, 0], [-1, 0], "10", [True, 0]])
def test_materialize_rejects_invalid_dna(space, bad):
    with pytest.raises((ValueError, TypeError)):
        space.materialize(bad)


def test_space_without_hyper_primitives_is_refused():
    with pytest.raises(ValueError, match="no hyper primitives"):
        ep.SearchSpace(helper.build_mlp(in_dim=8, hidden=16, out_dim=2))
    with pytest.raises(TypeError):
        ep.SearchSpace({"hidden": [16, 32]})


def test_candidate_must_be_fully_materialized():
    with pytest.raises(ValueError, match="hyper primitives"):
        ep.Candidate(helper.make_space())


def test_random_sample_with_a_seed_is_reproducible(space):
    a = space.sample(3, seed=7)
    b = space.sample(3, seed=7)
    assert len(a) == 3
    assert [c.dna for c in a] == [c.dna for c in b]
    assert [c.revision()["transform_revision"] for c in a] == [c.revision()["transform_revision"] for c in b]
    # the same stream pg.random_sample gives for the same seed
    expected = list(pg.random_sample(helper.make_space(), num_examples=3, seed=7))
    assert all(pg.eq(c.value, e) for c, e in zip(a, expected))
    # the seed matters
    assert len({tuple(tuple(c.dna) for c in space.sample(4, seed=s)) for s in range(10)}) > 1


def test_sample_requires_an_integer_seed(space):
    for bad in (None, True, 1.5, "7"):
        with pytest.raises(ValueError, match="seed"):
            space.sample(2, seed=bad)
    assert space.sample(0, seed=1) == []


# -- transforms ------------------------------------------------------------------------------


def test_string_patch_changes_the_built_module_and_the_revision(space):
    base = space.materialize([1, 0])
    wide = base.transform("widen?width=128")
    assert layer_shapes(wide.build()) == [(128, 8), (128,), (2, 128), (2,)]
    assert wide.transform_log == ["widen?width=128"]
    assert wide.dna == [1, 0]
    assert wide.revision()["patches"] == ["widen?width=128"]
    assert wide.revision()["transform_revision"] != base.revision()["transform_revision"]
    # the source candidate is untouched
    assert layer_shapes(base.build()) == [(32, 8), (32,), (2, 32), (2,)]
    assert base.transform_log == []
    assert base.revision()["patches"] == []


def test_dict_patches_chain_and_are_logged_in_order(space):
    base = space.materialize([1, 0])
    patched = base.transform([{"hidden": 8}, "widen?width=24"]).transform({"act": torch.nn.Tanh})
    module = patched.build()
    assert layer_shapes(module) == [(24, 8), (24,), (2, 24), (2,)]
    assert isinstance(module[1], torch.nn.Tanh)
    assert patched.transform_log[0] == {"hidden": 8}
    assert patched.transform_log[1] == "widen?width=24"
    assert json.loads(ep.canonical_json(patched.transform_log[2])) == patched.transform_log[2]
    assert len(patched.transform_log) == 3
    # the same patches in a different order are a different transform history
    other = base.transform(["widen?width=24", {"hidden": 8}])
    assert (
        other.revision()["transform_revision"]
        != base.transform([{"hidden": 8}, "widen?width=24"]).revision()["transform_revision"]
    )


def test_same_patches_give_the_same_revision(space):
    a = space.materialize([1, 0]).transform("widen?width=128")
    b = ep.SearchSpace(helper.make_space()).materialize([1, 0]).transform("widen?width=128")
    assert a.revision()["transform_revision"] == b.revision()["transform_revision"]


def test_unrecordable_or_failing_patches_are_refused(space):
    base = space.materialize([1, 0])
    with pytest.raises(ep.PatchError, match="unsupported patch"):
        base.transform(lambda k, v, p: v)
    with pytest.raises(ep.PatchError, match="no patches"):
        base.transform([])
    with pytest.raises(ep.PatchError, match="not registered"):
        base.transform("no_such_patcher?x=1")
    with pytest.raises(ep.PatchError):
        base.transform({"no_such_argument": 1})
    assert base.transform_log == []


def test_revision_record(space):
    candidate = space.materialize([2, 1]).transform("widen?width=40")
    rev = candidate.revision()
    assert set(rev) == {
        "adapter", "pyglove_version", "dna", "patches", "sym_hash", "builder_json", "transform_revision",
    }  # fmt: skip
    assert rev["adapter"] == "eidos-pyglove/0.1.0"
    assert rev["pyglove_version"] == pg.__version__ == ep.installed_pyglove_version()
    assert rev["dna"] == [2, 1]
    assert rev["builder_json"]["_type"] == "pg_mlp_helper.build_mlp"
    assert rev["builder_json"]["hidden"] == 40
    assert rev["transform_revision"] == ep.transform_revision(
        rev["builder_json"], rev["patches"], rev["pyglove_version"]
    )
    assert rev["transform_revision"].startswith("sha256:")
    assert rev["sym_hash"] == candidate.sym_hash


# -- export / load ---------------------------------------------------------------------------


def test_export_load_round_trip(space, tmp_path):
    candidate = space.materialize([1, 1]).transform("widen?width=48")
    path = tmp_path / "out" / "builder.json"
    revision = ep.export(candidate, path)
    on_disk = json.loads(path.read_text())
    assert on_disk["schema"] == "eidos-pyglove-builder/1"
    assert {k: v for k, v in on_disk.items() if k != "schema"} == revision
    assert [p.name for p in path.parent.iterdir()] == ["builder.json"]  # no temp file left behind

    loaded = ep.load(path, allowed_modules=ALLOWED)
    assert loaded.sym_hash == candidate.sym_hash
    assert pg.eq(loaded.value, candidate.value)
    assert loaded.revision() == revision
    assert loaded.dna == [1, 1]
    assert loaded.transform_log == ["widen?width=48"]
    assert str(loaded.build()) == str(candidate.build())
    assert layer_shapes(loaded.build()) == layer_shapes(candidate.build())
    # a loaded candidate keeps working as a transform source
    assert layer_shapes(loaded.transform("widen?width=5").build())[0] == (5, 8)


def test_builder_for_eidos_returns_a_builder_and_the_revision(space, tmp_path):
    candidate = space.materialize([0, 1])
    path = tmp_path / "b.json"
    exported = ep.export(candidate, path)
    build, revision = ep.builder_for_eidos(path, ALLOWED)
    assert revision == exported
    assert build.transform_revision == exported["transform_revision"]
    module = build()
    assert isinstance(module, torch.nn.Sequential) and isinstance(module[1], torch.nn.Tanh)
    assert tuple(module(torch.randn(3, 8)).shape) == (3, 2)
    assert build() is not module  # fresh each call
    assert str(build({"lr": 0.1})) == str(module)  # the Eidos build(config) shape; config is ignored
    # pinning the revision: the right one loads, any other is refused
    ep.builder_for_eidos(path, ALLOWED, expected_revision=exported["transform_revision"])
    with pytest.raises(ep.ExportIntegrityError, match="expected"):
        ep.builder_for_eidos(path, ALLOWED, expected_revision="sha256:" + "0" * 64)


def test_tampered_export_is_refused(space, tmp_path):
    path = tmp_path / "b.json"
    ep.export(space.materialize([1, 0]), path)

    def widen(doc):
        assert doc["builder_json"]["hidden"] == 32
        doc["builder_json"]["hidden"] = 4096

    rewrite_export(path, widen, rehash=False)
    with pytest.raises(ep.ExportIntegrityError, match="transform_revision mismatch"):
        ep.load(path, allowed_modules=ALLOWED)

    path2 = tmp_path / "b2.json"
    ep.export(space.materialize([1, 0]).transform("widen?width=64"), path2)
    rewrite_export(path2, lambda d: d["patches"].append("widen?width=1"), rehash=False)
    with pytest.raises(ep.ExportIntegrityError, match="mismatch"):
        ep.load(path2, allowed_modules=ALLOWED)

    path3 = tmp_path / "b3.json"
    ep.export(space.materialize([1, 0]), path3)
    rewrite_export(path3, lambda d: d.__setitem__("pyglove_version", "0.4.6"), rehash=False)
    with pytest.raises(ep.ExportIntegrityError, match="mismatch"):
        ep.load(path3, allowed_modules=ALLOWED)


@pytest.mark.parametrize(
    "content",
    ["", "not json", "[]", '{"schema": "other/1"}', '{"schema": "eidos-pyglove-builder/1"}', '{"x": NaN}'],
)
def test_malformed_exports_are_refused(tmp_path, content):
    path = tmp_path / "b.json"
    path.write_text(content)
    with pytest.raises(ep.ExportIntegrityError):
        ep.load(path, allowed_modules=ALLOWED)
    with pytest.raises(ep.ExportIntegrityError):
        ep.load(tmp_path / "missing.json", allowed_modules=ALLOWED)


def test_type_outside_allowed_modules_is_refused(space, tmp_path):
    path = tmp_path / "b.json"
    ep.export(space.materialize([1, 0]), path)
    # the builder's own module is not allowed
    with pytest.raises(ep.DisallowedTypeError, match="pg_mlp_helper.build_mlp"):
        ep.load(path, allowed_modules=["torch.nn"])
    # the activation class (a torch.nn type reference) is not allowed
    with pytest.raises(ep.DisallowedTypeError, match="torch.nn"):
        ep.load(path, allowed_modules=["pg_mlp_helper"])
    # allowed_modules is mandatory and never a bare string
    with pytest.raises(TypeError):
        ep.load(path, allowed_modules="pg_mlp_helper")
    with pytest.raises(TypeError):
        ep.load(path)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "hostile",
    [
        {"_type": "function", "name": "os.system"},
        {"_type": "type", "name": "subprocess.Popen"},
        {"_type": "os.system"},
        {
            "_type": "function",
            "name": "pg_mlp_helper.torch.load",
        },  # inside the allowed prefix, defined elsewhere
        {"_type": "function", "name": "pg_mlp_helper.torch.nope"},
        {"_type": "function", "name": "pg_mlp_helper.widen.__globals__"},
        {"_type": "function", "name": "torch.nn.modules.activation.ReLU\n"},
        {"_type": "function", "name": "pg_mlp_helper.<lambda>", "code": "AAAA", "defaults": None},
        {"_type": "function", "name": "torch.nn.ReLU", "code": "AAAA", "defaults": None},
        {"_type": "pyglove.core.utils.json_conversion._OpaqueObject", "value": "AAAA"},
        {"_type": 7},
        {"_type": "type"},
    ],
)
def test_hostile_files_with_a_valid_hash_are_refused(space, tmp_path, hostile):
    """A consistent file (hash recomputed) still cannot name anything outside the allowed modules."""
    path = tmp_path / "b.json"
    ep.export(space.materialize([1, 0]), path)
    rewrite_export(path, lambda d: d["builder_json"].__setitem__("act", hostile), rehash=True)
    with pytest.raises(ep.DisallowedTypeError):
        ep.load(path, allowed_modules=ALLOWED)


def test_hostile_nested_type_is_found_anywhere_in_the_tree(space, tmp_path):
    path = tmp_path / "b.json"
    ep.export(space.materialize([1, 0]), path)
    hostile = {"a": [1, {"b": {"_type": "function", "name": "os.system"}}]}
    rewrite_export(path, lambda d: d["builder_json"].__setitem__("extra", hostile), rehash=True)
    with pytest.raises(ep.DisallowedTypeError, match="os.system"):
        ep.load(path, allowed_modules=ALLOWED)


def test_load_does_not_import_modules_outside_the_allowed_ones(space, tmp_path):
    import sys

    path = tmp_path / "b.json"
    ep.export(space.materialize([1, 0]), path)
    marker = "eidos_pyglove_never_imported_module"
    rewrite_export(
        path,
        lambda d: d["builder_json"].__setitem__("act", {"_type": "type", "name": marker + ".X"}),
        rehash=True,
    )
    with pytest.raises(ep.DisallowedTypeError):
        ep.load(path, allowed_modules=ALLOWED)
    assert marker not in sys.modules


def test_unimportable_allowed_module_is_a_clear_error(space, tmp_path):
    path = tmp_path / "b.json"
    ep.export(space.materialize([1, 0]), path)
    with pytest.raises(ep.PyGloveAdapterError):
        ep.load(path, allowed_modules=["pg_mlp_helper", "torch.nn", "no_such_module_here"])


def test_export_refuses_marshalled_code(space, tmp_path):
    candidate = space.materialize([1, 0]).transform({"act": lambda: torch.nn.Tanh()})
    with pytest.raises(ep.DisallowedTypeError, match="marshalled code"):
        ep.export(candidate, tmp_path / "b.json")
    assert not (tmp_path / "b.json").exists()


# -- version pin -----------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["0.5.0", "0.4.4", "1.0", None])
def test_unsupported_pyglove_version_is_refused(monkeypatch, space, tmp_path, version):
    path = tmp_path / "b.json"
    ep.export(space.materialize([1, 0]), path)
    candidate = space.materialize([0, 0])
    monkeypatch.setattr(adapter, "installed_pyglove_version", lambda: version)
    with pytest.raises(ep.UnsupportedPyGloveError):
        ep.SearchSpace(helper.make_space())
    with pytest.raises(ep.UnsupportedPyGloveError):
        space.candidates()
    with pytest.raises(ep.UnsupportedPyGloveError):
        space.materialize([0, 0])
    with pytest.raises(ep.UnsupportedPyGloveError):
        candidate.build()
    with pytest.raises(ep.UnsupportedPyGloveError):
        ep.load(path, allowed_modules=ALLOWED)
    with pytest.raises(ep.UnsupportedPyGloveError):
        ep.builder_for_eidos(path, ALLOWED)


def test_export_written_by_an_unsupported_pyglove_is_refused(space, tmp_path):
    path = tmp_path / "b.json"
    ep.export(space.materialize([1, 0]), path)
    rewrite_export(path, lambda d: d.__setitem__("pyglove_version", "0.5.0"), rehash=True)
    with pytest.raises(ep.ExportIntegrityError, match="outside the supported range"):
        ep.load(path, allowed_modules=ALLOWED)
