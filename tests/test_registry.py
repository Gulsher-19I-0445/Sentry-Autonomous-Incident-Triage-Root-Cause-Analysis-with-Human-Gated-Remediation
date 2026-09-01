"""Tool registry consistency.

The registry is the seam where "code changed in one place but not the other"
shows up: a spec whose name drifts from its registry key means the model calls a
tool that dispatch() cannot find, and the only symptom is the agent quietly
burning steps on unknown-tool errors.

These tests derive everything from the registry itself, so they keep holding as
tools are added.
"""

import inspect

import pytest

from sentry.agent.tools import init as registry

REGISTRY = registry._REGISTRY
TOOL_NAMES = sorted(REGISTRY)


def test_registry_is_populated():
    assert TOOL_NAMES == [
        "get_metrics", "get_recent_changes", "search_logs", "search_runbooks"
    ]


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_spec_name_matches_registry_key(name):
    """The registry key is what dispatch() looks up; the spec name is what the
    model is told to call. They must be the same string."""
    spec, _ = REGISTRY[name]
    assert spec["toolSpec"]["name"] == name


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_spec_is_a_valid_converse_toolspec(name):
    """Converse rejects a malformed toolConfig with a ValidationException at the
    first call — i.e. after deploy, not before."""
    spec, _ = REGISTRY[name]

    assert set(spec) == {"toolSpec"}
    tool = spec["toolSpec"]
    assert set(tool) >= {"name", "description", "inputSchema"}

    assert isinstance(tool["name"], str) and tool["name"]
    assert isinstance(tool["description"], str)
    assert len(tool["description"]) > 40, "the description is all the model sees"

    schema = tool["inputSchema"]["json"]
    assert schema["type"] == "object"
    assert isinstance(schema["properties"], dict) and schema["properties"]
    assert isinstance(schema.get("required", []), list)


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_required_params_exist_in_properties(name):
    spec, _ = REGISTRY[name]
    schema = spec["toolSpec"]["inputSchema"]["json"]
    missing = set(schema.get("required", [])) - set(schema["properties"])
    assert not missing, f"{name} requires {missing} but does not declare them"


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_callable_accepts_every_declared_parameter(name):
    """Spec/implementation drift: the model sends what the spec advertises, and
    dispatch() calls fn(incident, **params). A property the function has no
    parameter for becomes an 'invalid arguments' error result on every call."""
    spec, fn = REGISTRY[name]
    params = inspect.signature(fn).parameters
    accepts_kwargs = any(p.kind is p.VAR_KEYWORD for p in params.values())

    positional = [
        p for p in params.values()
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    assert positional and positional[0].name == "incident", (
        f"{name} must take the incident as its first argument"
    )

    if accepts_kwargs:
        return
    for prop in spec["toolSpec"]["inputSchema"]["json"]["properties"]:
        assert prop in params, (
            f"{name} advertises {prop!r} in its spec but {fn.__name__}() has no "
            f"such parameter"
        )


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_declared_required_params_have_no_default_gap(name):
    """Every parameter the model may omit must have a default, or dispatch()
    turns an ordinary call into a TypeError error result."""
    spec, fn = REGISTRY[name]
    required = set(spec["toolSpec"]["inputSchema"]["json"].get("required", []))
    for pname, param in inspect.signature(fn).parameters.items():
        if pname == "incident" or param.kind is param.VAR_KEYWORD:
            continue
        if pname not in required:
            assert param.default is not inspect.Parameter.empty, (
                f"{name}.{pname} is optional to the model but has no default"
            )


def test_specs_returns_one_entry_per_registered_tool():
    specs = registry.specs()
    assert len(specs) == len(REGISTRY)
    assert {s["toolSpec"]["name"] for s in specs} == set(TOOL_NAMES)


def test_names_is_sorted():
    assert registry.names() == sorted(registry.names())


def test_dispatch_routes_to_the_registered_callable(monkeypatch, incident):
    seen = {}

    def fake_run(inc, **params):
        seen["incident"] = inc
        seen["params"] = params
        return {"ok": True}

    monkeypatch.setitem(registry._REGISTRY, "search_runbooks",
                        (REGISTRY["search_runbooks"][0], fake_run))

    result = registry.dispatch("search_runbooks", {"symptoms": "KeyError"}, incident)

    assert result == {"ok": True}
    assert seen["incident"] is incident
    assert seen["params"] == {"symptoms": "KeyError"}


def test_dispatch_returns_error_for_unknown_tool(incident):
    """A hallucinated tool name must come back as evidence the model can correct,
    not an exception that aborts the investigation."""
    result = registry.dispatch("delete_everything", {}, incident)

    assert "error" in result
    assert "delete_everything" in result["error"]
    # The available names are listed so the model can retry correctly.
    assert "search_logs" in result["error"]


def test_dispatch_returns_error_for_bad_arguments(incident):
    result = registry.dispatch("search_logs", {"not_a_real_param": 1}, incident)

    assert "error" in result
    assert "search_logs" in result["error"]


def test_dispatch_does_not_swallow_genuine_tool_bugs(monkeypatch, incident):
    """TypeError from argument binding is handled; a real bug inside a tool must
    still propagate, or a broken tool looks like a bad model call forever."""
    def exploding(inc, **params):
        raise RuntimeError("genuine bug")

    monkeypatch.setitem(registry._REGISTRY, "search_runbooks",
                        (REGISTRY["search_runbooks"][0], exploding))

    with pytest.raises(RuntimeError, match="genuine bug"):
        registry.dispatch("search_runbooks", {"symptoms": "x"}, incident)


def test_agent_never_registers_a_write_capable_tool():
    """Safety is enforced at the IAM boundary, but a tool named like a mutation
    is a design smell worth failing the build over."""
    forbidden = ("update", "delete", "put_", "create", "publish", "invoke", "shift")
    for name in TOOL_NAMES:
        assert not any(name.startswith(w) for w in forbidden), (
            f"{name} looks like a write operation; the agent is read-only"
        )
