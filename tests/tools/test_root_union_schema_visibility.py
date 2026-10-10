"""Root-union field visibility through the real model definitions and describe bridge."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.mcp_tool_schema import _convert_mcp_schema
from tools.schema_sanitizer import sanitize_tool_schemas


def _sanitize(schema):
    return sanitize_tool_schemas([{"function": {"name": "probe", "parameters": schema}}])[0]["function"]["parameters"]


@pytest.mark.parametrize("key", ["anyOf", "oneOf"])
def test_shared_conflicting_fields_and_required_intersection(key):
    shared = {"type": "string", "description": "Shared value."}
    schema = {key: [
        {"type": "object", "properties": {"shared": shared, "value": {"type": "string"},
                                           "left": {"type": "boolean"}},
         "required": ["shared", "value", "left"], "additionalProperties": False},
        {"type": "object", "properties": {"shared": shared, "value": {"type": "integer"},
                                           "right": {"type": "boolean"}},
         "required": ["shared", "value", "right"], "additionalProperties": False},
    ]}
    snapshot = copy.deepcopy(schema)
    out = _sanitize(schema)
    assert key not in out
    assert list(out["properties"]) == ["shared", "value", "left", "right"]
    assert out["properties"]["shared"] == shared
    assert out["properties"]["value"] == {"anyOf": [{"type": "string"}, {"type": "integer"}]}
    assert out["required"] == ["shared", "value"]
    assert "additionalProperties" not in out
    assert schema == snapshot
    assert _sanitize(schema) == out


@pytest.mark.parametrize("key", ["anyOf", "oneOf"])
def test_branch_boolean_required_does_not_become_global(key):
    schema = {key: [
        {"properties": {"shared": {"type": "string", "required": True},
                        "a": {"type": "string", "required": True}}},
        {"properties": {"shared": {"type": "string", "required": True},
                        "b": {"type": "integer", "required": True}}},
    ]}
    snapshot = copy.deepcopy(schema)
    out = _sanitize(schema)
    assert out["required"] == ["shared"]
    assert all("required" not in prop for prop in out["properties"].values())
    assert schema == snapshot


def test_hoisted_keys_round_trip_through_actual_argument_coercion(monkeypatch):
    from tools import arg_coercion
    from tools.registry import ToolRegistry
    schema = {"properties": {"a_b": {"type": "string"}}, "anyOf": [
        {"properties": {"a b": {"type": "string"}, "file path": {"type": "string"}}},
        {"properties": {"id": {"type": "string"}}},
    ]}
    registry = ToolRegistry()
    registry.register(name="probe", toolset="probe", schema={"name": "probe", "parameters": schema},
                      handler=lambda args: "{}")
    monkeypatch.setattr(arg_coercion, "registry", registry)
    assert "a_b_2" in _sanitize(schema)["properties"]
    assert arg_coercion.coerce_tool_args("probe", {"a_b": "root", "a_b_2": "branch", "file_path": "x"}) == {
        "a_b": "root", "a b": "branch", "file path": "x"}
    schema.pop("properties")
    assert arg_coercion.coerce_tool_args("probe", {"file_path": "x"}) == {"file path": "x"}


def test_root_constraints_win_and_nested_unions_are_unchanged():
    nested = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
    schema = {"type": "object", "properties": {"value": {"type": "string", "minLength": 3}},
              "required": ["value"], "additionalProperties": False,
              "oneOf": [{"properties": {"value": {"type": "integer"}, "nested": nested}},
                        {"properties": {"nested": nested}}]}
    out = _sanitize(schema)
    assert out["properties"]["value"] == schema["properties"]["value"]
    assert out["properties"]["nested"] == nested
    assert out["required"] == ["value"]
    assert out["additionalProperties"] is False


@pytest.mark.parametrize("other", [{}, {"type": "null"}, True, {"$ref": "#/$defs/Other"}])
def test_empty_nullable_or_opaque_branch_does_not_require_other_branch_fields(other):
    schema = {"anyOf": [{"type": "object", "properties": {"a": {"type": "string"}},
                         "required": ["a"]},
                        {"type": "object", "properties": {"b": {"type": "integer"}},
                         "required": ["b"]}, other]}
    out = _sanitize(schema)
    assert set(out["properties"]) == {"a", "b"}
    assert out["required"] == []


def test_property_refs_and_definitions_survive_hoisting():
    schema = {"$defs": {"Value": {"type": "string"}},
              "anyOf": [{"properties": {"value": {"$ref": "#/$defs/Value", "default": "x"}},
                         "required": ["value"]}, {"properties": {}}]}
    out = _sanitize(schema)
    assert out["$defs"] == schema["$defs"]
    assert out["properties"]["value"] == {"$ref": "#/$defs/Value"}
    assert out["required"] == []


@pytest.mark.parametrize("schema", [None, {}, {"type": "object"},
                                     {"type": "object", "properties": None, "required": True},
                                     {"anyOf": None}, {"oneOf": []}])
def test_adjacent_empty_and_malformed_schemas(schema):
    assert _sanitize(schema) == {"type": "object", "properties": {}, "required": []}


def test_nullable_root_keeps_existing_single_survivor_behavior():
    schema = {"anyOf": [{"type": "object", "properties": {"a": {"type": "string"}},
                         "required": ["a"]}, {"type": "null"}]}
    assert _sanitize(schema) == {"type": "object", "properties": {"a": {"type": "string"}},
                                 "required": ["a"], "nullable": True}


@pytest.mark.parametrize("quiet_mode", [False, True])
def test_whoop_fields_reach_actual_model_definitions_and_tool_describe(monkeypatch, quiet_mode):
    import model_tools
    from jsonschema import Draft202012Validator
    from tools.registry import ToolRegistry

    raw = json.loads((Path(__file__).parent / "fixtures" / "whoop_feedback_input_schema.json").read_text())
    snapshot = copy.deepcopy(raw)
    converted = _convert_mcp_schema("whoop-journal", SimpleNamespace(
        name="whoop_feedback", description="Submit feedback.", inputSchema=raw))
    registered_snapshot = copy.deepcopy(converted)
    registry = ToolRegistry()
    name = converted["name"]
    registry.register(name=name, toolset="mcp_schema_test", schema=converted, handler=lambda args: "{}")
    monkeypatch.setattr(model_tools, "registry", registry)
    monkeypatch.setattr("tools.registry.registry", registry)
    monkeypatch.setattr(model_tools, "_select_tool_names", lambda *args: [name])
    model_tools._clear_tool_defs_cache()
    try:
        definitions = model_tools.get_tool_definitions(quiet_mode=quiet_mode, skip_tool_search_assembly=True)
        again = model_tools.get_tool_definitions(quiet_mode=quiet_mode, skip_tool_search_assembly=True)
        assert again == definitions
        parameters = definitions[0]["function"]["parameters"]
        assert list(parameters["properties"]) == ["title", "description", "call_id", "confirm", "draft_id"]
        assert parameters["required"] == []
        assert "anyOf" not in parameters
        Draft202012Validator.check_schema(parameters)
        result, dispatch = model_tools._dispatch_bridge_tool("tool_describe", {"names": [name]}, None, None)
        assert dispatch is None
        described = json.loads(result)["tools"][name]["parameters"]
        assert described == parameters
        assert described["properties"]["confirm"] == {"type": "boolean", "const": True}
        assert described["properties"]["title"]["maxLength"] == 8000
        assert described["properties"]["description"]["maxLength"] == 16000
        assert described["properties"]["draft_id"]["pattern"] == raw["anyOf"][1]["properties"]["draft_id"]["pattern"]
        assert raw == snapshot
        assert converted == registered_snapshot
        validate_server = Draft202012Validator(converted["parameters"]).is_valid
        assert validate_server({"title": "Bug", "description": "Details"})
        assert not validate_server({"confirm": True})
        assert not validate_server({"title": "Bug", "description": "Details", "confirm": True})
    finally:
        model_tools._clear_tool_defs_cache()
