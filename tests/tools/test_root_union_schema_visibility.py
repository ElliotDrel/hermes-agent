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


def _dispatch_probe(monkeypatch, schema, args):
    """Exercise real model dispatch and registry with an offline server-boundary handler."""
    import model_tools
    from tools import arg_coercion
    from tools.registry import ToolRegistry

    received = []
    converted = _convert_mcp_schema("schema-test", SimpleNamespace(
        name="probe", description="Inspect arguments.", inputSchema=schema))
    registry = ToolRegistry()
    registry.register(name=converted["name"], toolset="mcp_schema_test", schema=converted,
                      handler=lambda args: received.append(copy.deepcopy(args)) or "{}")
    monkeypatch.setattr(model_tools, "registry", registry)
    monkeypatch.setattr(arg_coercion, "registry", registry)
    result = model_tools.handle_function_call(
        converted["name"], copy.deepcopy(args), skip_pre_tool_call_hook=True,
        skip_tool_request_middleware=True, skip_tool_execution_middleware=True)
    assert json.loads(result) == {}
    assert len(received) == 1
    return received[0]


@pytest.mark.parametrize("key", ["anyOf", "oneOf"])
def test_projected_boolean_coercion_reaches_dispatch(monkeypatch, key):
    from jsonschema import Draft202012Validator

    schema = {key: [
        {"type": "object", "properties": {"confirm flag": {"type": "boolean", "const": True}},
         "required": ["confirm flag"], "additionalProperties": False},
        {"type": "object", "properties": {"draft_id": {"type": "string"}},
         "required": ["draft_id"], "additionalProperties": False},
    ]}
    assert _sanitize(schema)["properties"]["confirm_flag"]["type"] == "boolean"
    received = _dispatch_probe(monkeypatch, schema, {"confirm_flag": "true"})
    assert received == {"confirm flag": True}
    Draft202012Validator(schema).validate(received)


@pytest.mark.parametrize("key", ["anyOf", "oneOf"])
@pytest.mark.parametrize("projected", [False, True])
@pytest.mark.parametrize("value,wire_key", [("text", "a b"), (7, "a@b")])
def test_nested_branch_renames_follow_exposed_keys(monkeypatch, key, projected, value, wire_key):
    from jsonschema import Draft202012Validator

    schema = {"anyOf": [
        {"properties": {"payload": {key: [
            {"type": "object", "properties": {"a b": {"type": "string"}},
             "required": ["a b"], "additionalProperties": False},
            {"type": "object", "properties": {"a@b": {"type": "integer"}},
             "required": ["a@b"], "additionalProperties": False},
        ]}}},
        {"properties": {"other": {"type": "string"}}},
    ]}
    if not projected:
        schema = schema["anyOf"][0]
    branches = _sanitize(schema)["properties"]["payload"][key]
    assert all(list(branch["properties"]) == ["a_b"] for branch in branches)
    received = _dispatch_probe(monkeypatch, schema, {"payload": {"a_b": value}})
    assert received == {"payload": {wire_key: value}}
    Draft202012Validator(schema).validate(received)


@pytest.mark.parametrize("reverse_order", [False, True])
@pytest.mark.parametrize("value,wire_key", [("text", "a b"), (7, "a@b")])
def test_conflicting_projected_payload_branches_reach_server(monkeypatch, reverse_order, value, wire_key):
    from jsonschema import Draft202012Validator

    variants = [{"type": "object", "properties": {"payload": {
        "type": "object", "properties": {name: {"type": kind}},
        "required": [name], "additionalProperties": False}}, "required": ["payload"]}
        for name, kind in [("a b", "string"), ("a@b", "integer")]]
    if reverse_order:
        variants.reverse()
    schema = {"anyOf": variants}
    exposed = _sanitize(schema)["properties"]["payload"]["anyOf"]
    assert all(list(branch["properties"]) == ["a_b"] for branch in exposed)
    received = _dispatch_probe(monkeypatch, schema, {"payload": {"a_b": value}})
    assert received == {"payload": {wire_key: value}}
    Draft202012Validator(schema).validate(received)


@pytest.mark.parametrize("types,value", [(["boolean", "string"], "true"),
                                        (["integer", "number"], "7"),
                                        (["object", "string"], '{"x": 1}'),
                                        (["array", "string"], "[1]")])
def test_conflicting_projected_types_are_not_coerced(monkeypatch, types, value):
    schema = {"anyOf": [{"properties": {"value": {"type": t}}} for t in types]}
    assert _dispatch_probe(monkeypatch, schema, {"value": value}) == {"value": value}


@pytest.mark.parametrize("reverse_order", [False, True])
@pytest.mark.parametrize("value", ["text", 7, True, None])
def test_ambiguous_or_unmatched_nested_rename_is_unchanged(monkeypatch, reverse_order, value):
    variants = [{"type": "object", "properties": {name: {"type": "string"}}}
                for name in ["a b", "a@b"]]
    if reverse_order:
        variants.reverse()
    schema = {"properties": {"payload": {"anyOf": variants}}}
    args = {"payload": {"a_b": value}}
    assert _dispatch_probe(monkeypatch, schema, args) == args


@pytest.mark.parametrize("evidence", ["const", "enum", "required", "additionalProperties"])
def test_nested_branch_schema_evidence_and_array_items(monkeypatch, evidence):
    variants = []
    for name, tag in [("a b", "left"), ("a@b", "right")]:
        branch = {"type": "object", "properties": {name: {"type": "integer"}}}
        if evidence in ("const", "enum"):
            branch["properties"]["kind"] = {evidence: tag if evidence == "const" else [tag]}
        else:
            branch["properties"][tag] = {"type": "string"}
            if evidence == "required":
                branch["required"] = [tag]
            else:
                branch["additionalProperties"] = False
        variants.append(branch)
    payload = {"a_b": 7, **({"kind": "right"} if evidence in ("const", "enum") else {"right": "x"})}
    schema = {"properties": {"payloads": {"type": "array", "items": {"oneOf": variants}}}}
    expected = {"payloads": [{"a@b": 7, **{k: v for k, v in payload.items() if k != "a_b"}}]}
    assert _dispatch_probe(monkeypatch, schema, {"payloads": [payload]}) == expected


@pytest.mark.parametrize("variants,value,expected", [
    ([{"properties": {"a b": {"type": "boolean"}}},
      {"properties": {"a@b": {"type": "integer"}}}], True, {"a b": True}),
    ([{"properties": {"a b": {"type": "string"}}},
      {"properties": {"a@b": {"type": "integer"}}}], "7", {"a b": "7"}),
    ([{"properties": {"a b": {"type": ["string", "null"]}}},
      {"properties": {"a@b": {"type": "null"}}}], None, {"a_b": None}),
    ([{"properties": {"a b": {"type": "integer"}, "a_b": {"type": "string"}}},
      {"properties": {"a@b": {"type": "string"}}, "additionalProperties": False}], 7, {"a b": 7}),
])
def test_nested_native_evidence_and_branch_local_suffixes(monkeypatch, variants, value, expected):
    key = "a_b_2" if "a_b" in variants[0]["properties"] else "a_b"
    schema = {"properties": {"payload": {"anyOf": variants}}}
    assert _dispatch_probe(monkeypatch, schema, {"payload": {key: value}}) == {"payload": expected}


def test_unrename_collision_preserves_both_supplied_keys(monkeypatch):
    schema = {"properties": {"payload": {"type": "object", "properties": {
        "a b": {"type": "string"}, "fixed key": {"type": "string"}}}}}
    args = {"payload": {"a_b": "model", "a b": "wire", "fixed_key": "fixed"}}
    assert _dispatch_probe(monkeypatch, schema, args) == {
        "payload": {"a_b": "model", "a b": "wire", "fixed key": "fixed"}}


def test_nested_identical_maps_and_opaque_branch(monkeypatch):
    branch = {"properties": {"a b": {"type": "string"}}}
    schema = {"properties": {"payload": {"anyOf": [branch, copy.deepcopy(branch)]}}}
    args = {"payload": {"a_b": "text", "unknown": "keep"}}
    assert _dispatch_probe(monkeypatch, schema, args) == {"payload": {"a b": "text", "unknown": "keep"}}
    schema["properties"]["payload"]["anyOf"].append({"$ref": "#/$defs/Other"})
    assert _dispatch_probe(monkeypatch, schema, args) == args


@pytest.mark.parametrize("key", ["anyOf", "oneOf"])
@pytest.mark.parametrize("projected", [False, True])
@pytest.mark.parametrize("fragment,value,expected", [
    (True, "keep", "keep"), (False, "keep", "keep"),
    ("boolean", "true", True), ("integer", "7", 7),
    ("object", '{"x": 1}', {"x": 1}), ("array", "[1]", [1]),
    ("unknown", '{"x": 1}', {"x": 1}),
])
def test_projected_raw_schema_fragments_reach_dispatch(monkeypatch, key, projected, fragment, value, expected):
    schema = {key: [{"properties": {"raw field": fragment}}, {"properties": {"other": {"type": "string"}}}]}
    if not projected:
        schema = schema[key][0]
    snapshot = copy.deepcopy(schema)
    assert "raw_field" in _sanitize(schema)["properties"]
    assert _dispatch_probe(monkeypatch, schema, {"raw_field": value}) == {"raw field": expected}
    assert schema == snapshot


@pytest.mark.parametrize("key", ["anyOf", "oneOf"])
@pytest.mark.parametrize("reverse_order", [False, True])
@pytest.mark.parametrize("shape", ["siblings", "common", "collision", "nested", "array", "renamed_container"])
def test_ambiguous_union_preserves_agreed_sibling_reversals(monkeypatch, key, reverse_order, shape):
    variants = [{"type": "object", "properties": {
        name: {"type": "string"}, "fixed key": {"type": "string"}}}
        for name in ["a b", "a@b"]]
    if reverse_order:
        variants.reverse()
    payload_schema: dict = {key: variants}
    payload: dict = {"a_b": "ambiguous", "fixed_key": "fixed", "unknown": "keep"}
    expected: dict = {"a_b": "ambiguous", "fixed key": "fixed", "unknown": "keep"}
    if shape == "common":
        for branch in variants:
            branch["properties"].pop("fixed key")
        payload_schema["properties"] = {"fixed key": {"type": "string"}}
    elif shape == "collision":
        for branch in variants:
            branch["properties"]["other key"] = {"type": "string"}
        payload.update({"fixed key": "supplied", "other_key": "other"})
        expected = {**payload, "other key": payload["other_key"]}
        expected.pop("other_key")
    elif shape == "nested":
        for branch in variants:
            branch["properties"]["child"] = {"properties": {"child key": {"type": "string"}}}
        payload["child"] = {"child_key": "child"}
        expected["child"] = {"child key": "child"}
    elif shape == "renamed_container":
        for branch, child_name in zip(variants, ["child key", "child@key"]):
            branch["properties"]["fixed key"] = {"properties": {child_name: {"type": "string"}}}
        payload["fixed_key"] = {"child_key": "ambiguous child"}
        expected["fixed key"] = payload["fixed_key"]
    schema: dict = {"properties": {"payload": payload_schema}}
    args, want = {"payload": payload}, {"payload": expected}
    if shape == "array":
        schema["properties"]["payload"] = {"type": "array", "items": payload_schema}
        args, want = {"payload": [payload]}, {"payload": [expected]}
    snapshot = copy.deepcopy(schema)
    assert _dispatch_probe(monkeypatch, schema, args) == want
    assert schema == snapshot


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
