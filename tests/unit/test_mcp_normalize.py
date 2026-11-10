"""Narrow MCP argument normalization (ADR 0017): exactly two recorded rules, applied only where a schema says so.

Pure unit tests (no database). The negative cases matter most: every ambiguous shape must be left untouched so the
lab refuses it, and the lab's own validators must be exactly as strict as before.
"""

from __future__ import annotations

import copy
import json

import pytest

from eidos.domain.contracts import validate_config
from eidos.mcp.gateway import ToolFailure, validate_args
from eidos.mcp.normalize import normalize, normalize_arguments
from eidos.mcp.tools import BY_NAME

CONFIG_SCHEMA = {"type": "object", "properties": {
    "argv": {"type": "array", "items": {"type": "string"}},
    "seed": {"type": "integer"},
    "timeout_seconds": {"type": "number"}}}

ARGV = ["python", "project.py", "train", "--inputs", "{work}", "--out", "{out}", "--seed", "{seed}"]

# The exact arguments MiniMax produced through Claude Code for the run_component MCP tool.
MANGLED = {"argv": {"item": ARGV}, "seed": "11", "timeout_seconds": "180"}


def untouched(value, schema, path=""):
    out, changes = normalize(value, schema, path)
    assert changes == [], changes
    assert out == value
    return out


# -- positives ---------------------------------------------------------------------------------------------------

def test_the_real_minimax_config_is_restored_and_every_change_is_recorded():
    original = copy.deepcopy(MANGLED)
    out, changes = normalize(MANGLED, CONFIG_SCHEMA, "config")
    assert out == {"argv": ARGV, "seed": 11, "timeout_seconds": 180}
    assert type(out["seed"]) is int and type(out["timeout_seconds"]) is int  # "180" is an integer literal
    by_path = {c["path"]: c for c in changes}
    assert set(by_path) == {"config.argv", "config.seed", "config.timeout_seconds"}
    assert by_path["config.argv"]["rule"] == "item-wrapper" and by_path["config.argv"]["expected"] == "array"
    assert by_path["config.seed"] == {"path": "config.seed", "rule": "numeric-string", "expected": "integer",
                                      "original": "11", "normalized": 11}
    assert by_path["config.timeout_seconds"] == {"path": "config.timeout_seconds", "rule": "numeric-string",
                                                 "expected": "number", "original": "180", "normalized": 180}
    assert original == MANGLED  # the caller's value is not mutated in place


def test_changes_are_json_serialisable_and_do_not_embed_the_whole_array():
    _, changes = normalize(MANGLED, CONFIG_SCHEMA, "config")
    text = json.dumps(changes)
    assert "<array of 9>" in text  # a compact description, not the payload
    assert "project.py" not in text


def test_the_config_is_validated_by_the_unchanged_lab_validator_only_after_normalization():
    assert validate_config(copy.deepcopy(MANGLED), CONFIG_SCHEMA) == [
        "config.argv: expected array, got dict",
        "config.seed: expected integer, got str",
        "config.timeout_seconds: expected number, got str"]
    out, _ = normalize(MANGLED, CONFIG_SCHEMA, "config")
    assert validate_config(out, CONFIG_SCHEMA) == []


def test_item_wrapper_nested_inside_array_items_and_objects():
    schema = {"type": "object", "properties": {
        "rows": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}},
        "opt": {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}}}}
    value = {"rows": [{"item": [1, 2]}, [3, 4], {"item": ["5", "6"]}], "opt": {"tags": {"item": ["a", "b"]}}}
    out, changes = normalize(value, schema, "config")
    assert out == {"rows": [[1, 2], [3, 4], [5, 6]], "opt": {"tags": ["a", "b"]}}
    paths = {(c["path"], c["rule"]) for c in changes}
    assert paths == {("config.rows[0]", "item-wrapper"), ("config.rows[2]", "item-wrapper"),
                     ("config.rows[2][0]", "numeric-string"), ("config.rows[2][1]", "numeric-string"),
                     ("config.opt.tags", "item-wrapper")}


def test_numeric_strings_inside_array_items():
    out, changes = normalize(["1", 2, "3"], {"type": "array", "items": {"type": "integer"}}, "xs")
    assert out == [1, 2, 3]
    assert [c["path"] for c in changes] == ["xs[0]", "xs[2]"]


@pytest.mark.parametrize("text, expected", [("0.5", 0.5), ("-3", -3), ("1e-3", 0.001), ("0", 0), ("2.50", 2.5),
                                            ("-0.0", -0.0), ("1E3", 1000.0), ("12", 12)])
def test_numeric_string_for_number(text, expected):
    out, changes = normalize(text, {"type": "number"}, "x")
    assert out == expected
    assert changes == [{"path": "x", "rule": "numeric-string", "expected": "number", "original": text,
                        "normalized": out}]


@pytest.mark.parametrize("text, expected", [("0", 0), ("-12", -12), ("7", 7), ("123456789012345678901", 123456789012345678901)])
def test_numeric_string_for_integer(text, expected):
    out, changes = normalize(text, {"type": "integer"}, "x")
    assert out == expected and type(out) is int
    assert changes[0]["rule"] == "numeric-string" and changes[0]["expected"] == "integer"
    assert changes[0]["original"] == text and changes[0]["normalized"] == expected


def test_empty_item_wrapper_becomes_an_empty_array():
    out, changes = normalize({"item": []}, {"type": "array", "items": {"type": "string"}}, "config.argv")
    assert out == [] and [c["rule"] for c in changes] == ["item-wrapper"]


def test_item_wrapper_without_items_schema_is_still_unwrapped_but_elements_are_not_touched():
    out, changes = normalize({"item": ["1", {"item": ["x"]}]}, {"type": "array"}, "p")
    assert out == ["1", {"item": ["x"]}]
    assert [c["rule"] for c in changes] == ["item-wrapper"]


def test_input_value_is_never_mutated_in_place():
    schema = {"type": "object", "properties": {"a": {"type": "array", "items": {"type": "integer"}},
                                               "b": {"type": "object", "properties": {"n": {"type": "number"}}}}}
    value = {"a": {"item": ["1", "2"]}, "b": {"n": "0.5"}, "extra": "9"}
    snapshot = copy.deepcopy(value)
    out, _ = normalize(value, schema)
    assert value == snapshot
    assert out == {"a": [1, 2], "b": {"n": 0.5}, "extra": "9"}  # a key outside the schema is passed through as is
    assert out is not value and out["b"] is not value["b"]


def test_unchanged_values_are_returned_as_is():
    value = {"argv": ["a"], "seed": 3}
    out, changes = normalize(value, CONFIG_SCHEMA, "config")
    assert out is value and changes == []


def test_normalize_arguments_applies_the_tool_schema_and_the_config_schema():
    tool_schema = BY_NAME["run_component"].input_schema()
    args = {"project": "p", "command_key": "k", "component_ref": "x@1.0.0", "seed": "7", "config": MANGLED}
    out, changes = normalize_arguments(args, tool_schema, CONFIG_SCHEMA)
    assert out["seed"] == 7
    assert out["config"] == {"argv": ARGV, "seed": 11, "timeout_seconds": 180}
    assert {c["path"] for c in changes} == {"seed", "config.argv", "config.seed", "config.timeout_seconds"}
    assert args["seed"] == "7" and args["config"] is MANGLED  # input untouched
    # without a config schema only the tool's own schema applies: config is an unconstrained object
    out2, changes2 = normalize_arguments(args, tool_schema)
    assert out2["config"] == MANGLED and [c["path"] for c in changes2] == ["seed"]


# -- negatives: ambiguous shapes stay untouched ----------------------------------------------------------------

ARRAY = {"type": "array", "items": {"type": "string"}}


@pytest.mark.parametrize("value", [
    {"item": "python"},                       # scalar value: a one-element list cannot be inferred
    {"item": ["a"], "other": 1},              # the wrapper must be the object's only key
    {"items": ["a", "b"]},                    # a different key
    {"Item": ["a"]},                          # case matters
    {"item": None},
    {"item": {"item": ["a"]}},                # only one level, and the inner value is not a list
    {},
    {"item": 3},
])
def test_ambiguous_wrappers_are_left_for_the_lab_to_refuse(value):
    untouched(value, ARRAY, "argv")


@pytest.mark.parametrize("schema", [
    {"type": "object"},                       # an object: {"item": [...]} is a legitimate value
    {"type": "object", "properties": {"item": {"type": "array"}}},
    {},                                       # no type
    {"type": ["array", "null"]},              # a list of types is not recognised
    {"type": "string"},
    {"type": "ARRAY"},
    {"items": {"type": "string"}},
    None,
])
def test_item_wrapper_is_only_undone_where_the_schema_says_array(schema):
    untouched({"item": ["a", "b"]}, schema, "p")


@pytest.mark.parametrize("text", [" 11", "11 ", "011", "+1", "1.0", "1e2", "-0 ", "\n7", "7\n", "1,000", "0x10",
                                  "1_000", "", " ", "NaN", "Infinity", "-Infinity", "١٢", "１２", "٣", "--1", "1-",
                                  "1.", ".5", "e5", "-", "true", "null"])
def test_non_canonical_strings_are_not_integers(text):
    untouched(text, {"type": "integer"}, "n")


@pytest.mark.parametrize("text", [" 0.5", "0.5 ", "011", "+1", "+1.5", ".5", "1.", "1e", "1e+", "NaN", "nan",
                                  "Infinity", "-Infinity", "inf", "1e400", "-1e400", "0x10", "1_000", "", " ",
                                  "١٢", "١.٥", "１２", "1,5", "1.5.2", "--1", "true", "null", "5 apples"])
def test_non_canonical_or_non_finite_strings_are_not_numbers(text):
    untouched(text, {"type": "number"}, "n")


@pytest.mark.parametrize("value", [True, False, None, 1, 1.5, [], {}, ["1"], {"item": ["1"]}])
def test_non_string_values_are_never_converted_to_integers(value):
    untouched(value, {"type": "integer"}, "n")


@pytest.mark.parametrize("value", ["11", "0.5", "-3", "1e-3", "0"])
def test_numeric_string_is_kept_where_the_schema_says_string(value):
    untouched(value, {"type": "string"}, "name")


@pytest.mark.parametrize("schema", [{}, {"type": ["integer", "null"]}, {"type": ["number", "string"]},
                                    {"type": "boolean"}, {"type": "object"}, {"type": "array"}, None])
def test_numeric_string_is_kept_without_a_recognised_numeric_schema(schema):
    untouched("11", schema, "n")


def test_a_value_with_no_schema_or_not_in_the_schema_is_untouched():
    value = {"known": "1", "unknown": "2", "unknown_wrapper": {"item": ["a"]}}
    schema = {"type": "object", "properties": {"known": {"type": "string"}}}
    untouched(value, schema, "config")
    untouched(value, None, "config")
    untouched(value, {}, "config")
    untouched(value, {"type": "object"}, "config")  # no properties: nothing to look up
    untouched(value, {"type": "object", "properties": {"known": {}}}, "config")


def test_a_wrapper_with_the_wrong_schema_type_does_not_unwrap_nested_elements_either():
    schema = {"type": "object", "properties": {"argv": {"type": "string"}, "n": {"type": "boolean"}}}
    untouched({"argv": {"item": ["a"]}, "n": "1"}, schema, "config")


def test_one_ambiguous_field_does_not_block_the_unambiguous_ones():
    cfg = {"argv": {"item": "python"}, "seed": "11", "timeout_seconds": "1e400"}
    out, changes = normalize(cfg, CONFIG_SCHEMA, "config")
    assert out == {"argv": {"item": "python"}, "seed": 11, "timeout_seconds": "1e400"}
    assert [c["path"] for c in changes] == ["config.seed"]
    # and the lab validator still reports exactly the fields that stayed ambiguous
    assert validate_config(out, CONFIG_SCHEMA) == ["config.argv: expected array, got dict",
                                                   "config.timeout_seconds: expected number, got str"]


def test_numeric_strings_up_to_the_length_bound_are_converted_and_longer_ones_left_alone():
    from eidos.mcp.normalize import MAX_NUMERIC_STRING
    ok = "9" * MAX_NUMERIC_STRING
    out, changes = normalize(ok, {"type": "integer"}, "n")
    assert out == int(ok) and changes[0]["original"] == ok
    untouched("9" * (MAX_NUMERIC_STRING + 1), {"type": "integer"}, "n")


@pytest.mark.parametrize("want", ["integer", "number"])
def test_an_absurdly_long_digit_string_is_left_alone(want):
    untouched("9" * 5000, {"type": want}, "n")


@pytest.mark.parametrize("args", [{}, {"config": None}, {"config": "text"}, {"config": ["a"]}])
def test_normalize_arguments_ignores_a_missing_or_non_object_config(args):
    out, changes = normalize_arguments(args, {"type": "object", "properties": {}}, CONFIG_SCHEMA)
    assert out == args and changes == []


# -- the lab-side validators are not relaxed --------------------------------------------------------------------

def test_lab_validate_config_is_unchanged_for_the_original_and_the_normalized_config():
    problems = validate_config(copy.deepcopy(MANGLED), CONFIG_SCHEMA)
    assert problems == ["config.argv: expected array, got dict", "config.seed: expected integer, got str",
                        "config.timeout_seconds: expected number, got str"]
    out, _ = normalize(MANGLED, CONFIG_SCHEMA, "config")
    assert validate_config(out, CONFIG_SCHEMA) == []


def test_validate_config_still_rejects_every_shape_the_normalizer_leaves_alone():
    for bad in ({"argv": {"item": "python"}}, {"argv": {"item": ["a"], "x": 1}}, {"seed": " 11"}, {"seed": "1.0"},
                {"seed": True}, {"timeout_seconds": "NaN"}, {"timeout_seconds": "0x10"}):
        out, changes = normalize(bad, CONFIG_SCHEMA, "config")
        assert changes == [] and validate_config(out, CONFIG_SCHEMA), bad


def test_gateway_validate_args_still_rejects_wrapped_arrays_and_numeric_strings():
    compare = BY_NAME["compare_trials"]
    validate_args(compare, {"project": "p", "job_ids": ["a", "b"]})
    with pytest.raises(ToolFailure) as e:
        validate_args(compare, {"project": "p", "job_ids": {"item": "x"}})
    assert e.value.code == "INVALID_ARGUMENTS" and "'job_ids' must be array" in e.value.message
    with pytest.raises(ToolFailure) as e:
        validate_args(compare, {"project": "p", "job_ids": {"item": ["a", "b"]}})  # validate_args itself never unwraps
    assert "'job_ids' must be array" in e.value.message
    events = BY_NAME["list_events"]
    validate_args(events, {"project": "p", "limit": 5})
    for bad in ("1.5", "5", "1e2"):
        with pytest.raises(ToolFailure) as e:
            validate_args(events, {"project": "p", "limit": bad})
        assert "'limit' must be integer" in e.value.message
    with pytest.raises(ToolFailure):
        validate_args(events, {"project": "p", "limit": True})
