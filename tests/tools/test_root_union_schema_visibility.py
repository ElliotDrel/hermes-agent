"""Root-union visibility and wire ownership through real model-tool consumers."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

from jsonschema import Draft202012Validator

from tools.mcp_tool_schema import _convert_mcp_schema
from tools.registry import ToolRegistry


def test_whoop_union_survives_definitions_cache_and_tool_describe(monkeypatch):
    import model_tools

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
        uncached = model_tools.get_tool_definitions(quiet_mode=False, skip_tool_search_assembly=True)
        cached = model_tools.get_tool_definitions(quiet_mode=True, skip_tool_search_assembly=True)
        cache_hit = model_tools.get_tool_definitions(quiet_mode=True, skip_tool_search_assembly=True)
        assert uncached == cached == cache_hit
        parameters = uncached[0]["function"]["parameters"]
        # Field constraints come from the server, not a snapshot of today's catalog.
        server_fields = {**raw["anyOf"][0]["properties"], **raw["anyOf"][1]["properties"]}
        assert set(server_fields) == {"title", "description", "call_id", "confirm", "draft_id"}
        assert parameters["properties"] == server_fields
        assert parameters["required"] == []
        assert "anyOf" not in parameters
        Draft202012Validator.check_schema(parameters)
        result = model_tools.handle_function_call("tool_describe", {"names": [name]},
            skip_pre_tool_call_hook=True, skip_tool_request_middleware=True,
            skip_tool_execution_middleware=True)
        assert json.loads(result)["tools"][name]["parameters"] == parameters
        assert raw == snapshot
        assert registry.get_schema(name) == registered_snapshot
        server = Draft202012Validator(registry.get_schema(name)["parameters"])
        draft = {"title": "Bug", "description": "Details"}
        confirmation = {"confirm": True, "draft_id": "12345678-1234-1234-1234-123456789abc"}
        server.validate(draft)
        server.validate(confirmation)
        assert not server.is_valid({"confirm": True})
        assert not server.is_valid({**draft, **confirmation})
    finally:
        model_tools._clear_tool_defs_cache()


def test_projected_types_dispatch_without_stealing_or_guessing_wire_fields(monkeypatch):
    import model_tools
    from tools import arg_coercion

    owned = {"type": "object", "properties": {"a_b": {"type": "integer"}},
             "required": ["a_b"], "anyOf": [
                 {"properties": {"a b": {"type": "string"}, "fixed key": {"type": "string"}}},
                 {"properties": {"a@b": {"type": "integer"}, "fixed key": {"type": "string"}}},
             ]}
    selected = {"type": "object", "oneOf": [
        {"properties": {"mode": {"anyOf": [{"const": "left"}, {"type": "null"}]},
                        "a b": {"type": "string"}}, "required": ["mode", "a b"], "additionalProperties": False},
        {"properties": {"mode": {"anyOf": [{"const": "right"}, {"type": "null"}]},
                        "a@b": {"type": "string"}}, "required": ["mode", "a@b"], "additionalProperties": False},
    ]}
    ambiguous = {"type": "object", "anyOf": [
        {"properties": {"a b": {"type": "string"}, "fixed key": {"type": "string"}},
         "required": ["a b"], "additionalProperties": False},
        {"properties": {"a@b": {"type": "string"}, "fixed key": {"type": "string"}},
         "required": ["a@b"], "additionalProperties": False},
    ]}
    common = {"owned": owned, "selected": selected, "ambiguous": ambiguous}
    raw = {"oneOf": [
        {"type": "object", "properties": {**common, "mode": {"const": "now"},
            "confirm flag": "boolean",
            "nullable flag": {"type": ["boolean", "null"]}}, "required": ["mode", "confirm flag"]},
        {"type": "object", "properties": {**common, "mode": {"const": "draft"},
            "confirm flag": {"type": "boolean", "enum": [True, False]},
            "nullable flag": {"type": "boolean", "const": True}}, "required": ["mode", "confirm flag"]},
    ]}
    snapshot = copy.deepcopy(raw)
    # Register raw fragments directly: MCP conversion already collapses nullable
    # discriminators and would hide the model/dispatch normalization mismatch.
    converted = {"name": "normalization_probe", "description": "Inspect arguments.", "parameters": raw}
    received = []
    registered_snapshot = copy.deepcopy(converted)
    registry = ToolRegistry()
    name = converted["name"]
    registry.register(name=name, toolset="mcp_schema_test", schema=converted,
        handler=lambda args: received.append(copy.deepcopy(args)) or "{}")
    monkeypatch.setattr(model_tools, "registry", registry)
    monkeypatch.setattr(arg_coercion, "registry", registry)
    monkeypatch.setattr(model_tools, "_select_tool_names", lambda *args: [name])
    parameters = model_tools.get_tool_definitions(quiet_mode=False, skip_tool_search_assembly=True)[0]["function"]["parameters"]
    exposed = parameters["properties"]
    assert exposed["confirm_flag"]["type"] == raw["oneOf"][0]["properties"]["confirm flag"]
    assert exposed["confirm_flag"]["anyOf"] == [
        {"type": "boolean"}, raw["oneOf"][1]["properties"]["confirm flag"]]
    assert exposed["nullable_flag"] == {"type": "boolean", "nullable": True, "anyOf": [
        {"type": "boolean", "nullable": True}, raw["oneOf"][1]["properties"]["nullable flag"]]}
    assert exposed["owned"]["properties"]["a_b"] == owned["properties"]["a_b"]
    assert exposed["selected"]["oneOf"][1]["properties"]["a_b"] == selected["oneOf"][1]["properties"]["a@b"]
    assert exposed["selected"]["oneOf"][1]["properties"]["mode"] == {"const": "right", "nullable": True}
    assert parameters["required"] == ["mode", "confirm_flag"]
    Draft202012Validator.check_schema(parameters)

    args = {"mode": "now", "confirm_flag": "true", "nullable_flag": "true",
            "owned": {"a_b": 7, "fixed_key": "owned sibling"}, "selected": {"mode": "right", "a_b": "chosen"}}
    result = model_tools.handle_function_call(name, copy.deepcopy(args),
        skip_pre_tool_call_hook=True, skip_tool_request_middleware=True,
        skip_tool_execution_middleware=True)
    assert json.loads(result) == {}
    wire = {"mode": "now", "confirm flag": True, "nullable flag": True,
            "owned": {"a_b": 7, "fixed key": "owned sibling"}, "selected": {"mode": "right", "a@b": "chosen"}}
    assert received == [wire]
    # Bare-string shorthand is not JSON Schema; spell that fragment out solely
    # for validation, retaining the server's unions and every real constraint.
    validation_schema = copy.deepcopy(raw)
    validation_schema["oneOf"][0]["properties"]["confirm flag"] = {"type": "boolean"}
    server = Draft202012Validator(validation_schema)
    server.validate(wire)

    # Same model spelling and type fit both wire names: do not guess, but still
    # reverse the agreed sibling. The original server, not the projection, rejects it.
    args["ambiguous"] = {"a_b": "uncertain", "fixed_key": "agreed sibling"}
    args["nullable_flag"] = "null"
    args["selected"] = {"mode": None, "a_b": "uncertain"}
    result = model_tools.handle_function_call(name, copy.deepcopy(args),
        skip_pre_tool_call_hook=True, skip_tool_request_middleware=True,
        skip_tool_execution_middleware=True)
    assert json.loads(result) == {}
    ambiguous_wire = {**wire, "nullable flag": None, "selected": {"mode": None, "a_b": "uncertain"},
                      "ambiguous": {"a_b": "uncertain", "fixed key": "agreed sibling"}}
    assert received == [wire, ambiguous_wire]
    assert not server.is_valid(ambiguous_wire)
    assert raw == snapshot
    assert registry.get_schema(name) == registered_snapshot
