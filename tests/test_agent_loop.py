"""The Converse tool-use loop, driven by a scripted stub.

`run_agent` is the one piece of this system that is genuinely stateful: it
accumulates messages, folds tool results back in, and decides when to stop. It
also costs ~$0.15 to exercise for real, which is why none of it was tested until
now.

Every test here replaces `bedrock._converse` with a scripted list of responses,
so the loop runs end to end with no Bedrock call.
"""

import json

import pytest

from sentry.agent import bedrock
from sentry.agent.bedrock import (
    AgentRun,
    MaxStepsExceeded,
    extract_json,
    run_agent,
)


# --------------------------------------------------------------------------- #
# response builders — the shape Converse actually returns
# --------------------------------------------------------------------------- #

def tool_use_response(name, tool_input, use_id="tu-1", tokens=(1000, 50)):
    return {
        "output": {"message": {"content": [
            {"text": f"Let me check {name}."},
            {"toolUse": {"toolUseId": use_id, "name": name, "input": tool_input}},
        ]}},
        "stopReason": "tool_use",
        "usage": {"inputTokens": tokens[0], "outputTokens": tokens[1]},
    }


def text_response(text, stop_reason="end_turn", tokens=(2000, 200)):
    return {
        "output": {"message": {"content": [{"text": text}]}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": tokens[0], "outputTokens": tokens[1]},
    }


@pytest.fixture
def script(monkeypatch):
    """Install a scripted _converse; returns the recorded call list."""
    calls = []

    def install(responses):
        queue = list(responses)

        def fake_converse(model_id, messages, system, tools, max_attempts=5):
            calls.append({
                "model_id": model_id,
                "messages": json.loads(json.dumps(messages, default=str)),
                "system": system,
                "tools": tools,
            })
            if not queue:
                raise AssertionError("loop called _converse more times than scripted")
            return queue.pop(0)

        monkeypatch.setattr(bedrock, "_converse", fake_converse)
        return calls

    return install


FINAL_JSON = json.dumps({
    "root_cause_category": "code_defect",
    "summary": "KeyError after version 7.",
    "confidence": 0.8,
    "evidence": ["14 KeyErrors in the api log group"],
    "suspect_change": "version 7",
    "proposed_remediation": "alias_rollback",
    "needs_human_investigation": False,
})


# --------------------------------------------------------------------------- #
# the scripted sequence
# --------------------------------------------------------------------------- #

def test_scripted_tool_sequence_executes_in_order(script):
    calls = script([
        tool_use_response("search_logs", {"log_group": "api"}, "tu-1"),
        tool_use_response("get_metrics", {"target": "api"}, "tu-2"),
        text_response(FINAL_JSON),
    ])

    executed = []

    def executor(name, params):
        executed.append((name, params))
        return {"result_count": 3}

    run = run_agent("system", "user", tools=[{"toolSpec": {"name": "search_logs"}}],
                    executor=executor)

    assert executed == [
        ("search_logs", {"log_group": "api"}),
        ("get_metrics", {"target": "api"}),
    ]
    assert [s.name for s in run.steps] == ["search_logs", "get_metrics"]
    assert run.final_text == FINAL_JSON
    assert run.stop_reason == "end_turn"
    assert len(calls) == 3


def test_tool_results_are_fed_back_to_the_model(script):
    calls = script([
        tool_use_response("search_logs", {"log_group": "api"}, "tu-abc"),
        text_response(FINAL_JSON),
    ])

    run_agent("system", "user", tools=[], executor=lambda n, p: {"rows": 4})

    # Second call carries: user prompt, assistant tool_use, user tool_result.
    second = calls[1]["messages"]
    assert len(second) == 3
    assert second[0]["role"] == "user"
    assert second[1]["role"] == "assistant"

    result_block = second[2]["content"][0]["toolResult"]
    assert result_block["toolUseId"] == "tu-abc", "toolUseId must be echoed exactly"
    assert result_block["status"] == "success"
    assert result_block["content"][0]["json"]["result"] == {"rows": 4}


def test_non_dict_tool_result_is_sent_as_text(script):
    calls = script([
        tool_use_response("search_runbooks", {"symptoms": "x"}),
        text_response(FINAL_JSON),
    ])

    run_agent("system", "user", tools=[], executor=lambda n, p: "no match")

    block = calls[1]["messages"][2]["content"][0]["toolResult"]["content"][0]
    assert block == {"text": "no match"}


def test_token_counts_accumulate_across_turns(script):
    script([
        tool_use_response("search_logs", {"log_group": "api"}, tokens=(1000, 50)),
        tool_use_response("get_metrics", {"target": "api"}, tokens=(3000, 60)),
        text_response(FINAL_JSON, tokens=(5000, 300)),
    ])

    run = run_agent("system", "user", tools=[], executor=lambda n, p: {"ok": True})

    assert run.input_tokens == 9000
    assert run.output_tokens == 410


def test_cost_reflects_accumulated_tokens(script):
    script([text_response(FINAL_JSON, tokens=(1_000_000, 1_000_000))])

    run = run_agent("system", "user", tools=[], executor=lambda n, p: None,
                    model_id="us.anthropic.claude-sonnet-5")

    # 1M input at $3 + 1M output at $15.
    assert run.cost_usd == pytest.approx(18.0)


# --------------------------------------------------------------------------- #
# prompt caching
# --------------------------------------------------------------------------- #

def test_no_cache_points_when_disabled(script, monkeypatch):
    """Off by default — the request shape is validated server-side, so a wrong
    one fails the whole investigation rather than degrading."""
    monkeypatch.setattr(bedrock.Config, "ENABLE_PROMPT_CACHE", False)
    calls = script([text_response(FINAL_JSON)])

    bedrock.run_agent("system", "user", tools=[], executor=lambda n, p: None)

    assert json.dumps(calls[0]).count("cachePoint") == 0


def test_cache_points_mark_system_and_the_newest_turn(monkeypatch):
    """Two breakpoints: the system prompt (stable, and it covers tools since
    Converse renders those first) and the end of the latest message, so each
    turn reads what the previous one wrote."""
    monkeypatch.setattr(bedrock.Config, "ENABLE_PROMPT_CACHE", True)
    captured = {}

    def fake_client_converse(**kwargs):
        captured.update(kwargs)
        return text_response(FINAL_JSON)

    monkeypatch.setattr(bedrock._client, "converse", fake_client_converse)

    bedrock._converse("m", [{"role": "user", "content": [{"text": "hi"}]}], "sys", None)

    assert captured["system"][-1] == bedrock.CACHE_POINT
    assert captured["messages"][-1]["content"][-1] == bedrock.CACHE_POINT


def test_cache_points_do_not_accumulate_in_the_transcript(script, monkeypatch):
    """The caller's message list is reused across turns. Appending a cache point
    to it in place would add one per turn and blow the 4-breakpoint limit."""
    monkeypatch.setattr(bedrock.Config, "ENABLE_PROMPT_CACHE", True)
    calls = script([
        tool_use_response("search_logs", {"log_group": "api"}),
        tool_use_response("get_metrics", {"target": "api"}),
        text_response(FINAL_JSON),
    ])

    bedrock.run_agent("system", "user", tools=[], executor=lambda n, p: {"ok": True})

    for call in calls:
        for message in call["messages"]:
            content = message.get("content", [])
            points = [b for b in content if b == bedrock.CACHE_POINT]
            assert len(points) <= 1, "a turn accumulated more than one cache point"


def test_cached_tokens_accumulate_and_are_priced_separately(script):
    """Bedrock reports cached tokens outside inputTokens. Pricing them at the
    full input rate would report a cached run as costing an uncached one."""
    script([{
        "output": {"message": {"content": [{"text": FINAL_JSON}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 1_000_000, "outputTokens": 0,
                  "cacheReadInputTokens": 1_000_000,
                  "cacheWriteInputTokens": 1_000_000},
    }])

    run = run_agent("system", "user", tools=[], executor=lambda n, p: None,
                    model_id="us.anthropic.claude-sonnet-5")

    assert run.cache_read_tokens == 1_000_000
    assert run.cache_write_tokens == 1_000_000
    # 1M input at $3 + 1M read at 0.1x + 1M write at 1.25x
    assert run.cost_usd == pytest.approx(3.0 + 0.3 + 3.75)


def test_absent_cache_usage_is_treated_as_zero(script):
    """An uncached response omits the fields entirely."""
    script([text_response(FINAL_JSON, tokens=(1000, 100))])

    run = run_agent("system", "user", tools=[], executor=lambda n, p: None)

    assert run.cache_read_tokens == 0
    assert run.to_dict()["cache_write_tokens"] == 0


def test_missing_usage_block_does_not_crash_the_run(script):
    """Not every Converse response carries usage; a KeyError here would lose a
    completed investigation over an accounting detail."""
    script([{"output": {"message": {"content": [{"text": FINAL_JSON}]}},
             "stopReason": "end_turn"}])

    run = run_agent("system", "user", tools=[], executor=lambda n, p: None)

    assert run.input_tokens == 0
    assert run.final_text == FINAL_JSON


def test_multiple_tool_uses_in_one_turn_all_execute(script):
    """The model can request several tools in a single message."""
    parallel = {
        "output": {"message": {"content": [
            {"toolUse": {"toolUseId": "a", "name": "search_logs", "input": {}}},
            {"toolUse": {"toolUseId": "b", "name": "get_metrics", "input": {}}},
        ]}},
        "stopReason": "tool_use",
        "usage": {"inputTokens": 100, "outputTokens": 10},
    }
    calls = script([parallel, text_response(FINAL_JSON)])

    executed = []
    run_agent("system", "user", tools=[],
              executor=lambda n, p: executed.append(n) or {"ok": True})

    assert executed == ["search_logs", "get_metrics"]
    assert len(calls[1]["messages"][2]["content"]) == 2


# --------------------------------------------------------------------------- #
# failure modes
# --------------------------------------------------------------------------- #

def test_max_tokens_stop_raises(script):
    """A truncated final message is usually half a JSON object. Escalating beats
    handing the repair path something unparseable."""
    script([text_response('{"root_cause_category": "code_def',
                          stop_reason="max_tokens")])

    with pytest.raises(MaxStepsExceeded, match="truncated"):
        run_agent("system", "user", tools=[], executor=lambda n, p: None)


def test_step_limit_raises(script):
    """A model that keeps calling tools forever must stop costing money."""
    script([tool_use_response("search_logs", {"log_group": "api"})] * 3)

    with pytest.raises(MaxStepsExceeded, match="no conclusion after 3 steps"):
        run_agent("system", "user", tools=[], executor=lambda n, p: {"ok": True},
                  max_steps=3)


def test_tool_failure_is_recorded_as_evidence_not_a_crash(script):
    """A broken tool must not abort the investigation — the model should see the
    error and route around it."""
    calls = script([
        tool_use_response("search_logs", {"log_group": "api"}, "tu-x"),
        text_response(FINAL_JSON),
    ])

    def exploding(name, params):
        raise RuntimeError("Insights query failed")

    run = run_agent("system", "user", tools=[], executor=exploding)

    assert run.steps[0].error == "RuntimeError: Insights query failed"
    assert run.steps[0].result is None

    result_block = calls[1]["messages"][2]["content"][0]["toolResult"]
    assert result_block["status"] == "error"
    assert "Insights query failed" in result_block["content"][0]["text"]
    assert run.final_text == FINAL_JSON


def test_tools_are_omitted_from_the_request_when_empty(script):
    """The repair call passes tools=[] — Converse rejects an empty toolConfig."""
    calls = script([text_response(FINAL_JSON)])

    run_agent("system", "user", tools=[], executor=lambda n, p: None)

    assert calls[0]["tools"] == []


def test_trace_preview_is_bounded(script):
    """to_dict() is written to DynamoDB, which has a 400KB item limit."""
    script([
        tool_use_response("search_logs", {"log_group": "api"}),
        text_response(FINAL_JSON),
    ])

    run = run_agent("system", "user", tools=[],
                    executor=lambda n, p: {"entries": ["x" * 100] * 200})

    preview = run.to_dict()["steps"][0]["result_preview"]
    assert len(preview) <= 500


# --------------------------------------------------------------------------- #
# extract_json
# --------------------------------------------------------------------------- #

def test_extract_json_bare():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fenced():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_fenced_without_language():
    assert extract_json('```\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_with_surrounding_prose():
    """The prompt forbids prose, and the model produces it anyway."""
    assert extract_json('Here is my analysis:\n{"a": 1}\nHope that helps.') == {"a": 1}


def test_extract_json_nested_objects():
    text = '{"outer": {"inner": [1, 2]}, "b": null}'
    assert extract_json(text) == {"outer": {"inner": [1, 2]}, "b": None}


def test_extract_json_raises_when_absent():
    with pytest.raises(ValueError, match="no JSON object found"):
        extract_json("I was unable to determine a root cause.")


def test_extract_json_raises_on_malformed():
    with pytest.raises(json.JSONDecodeError):
        extract_json('{"a": 1,,}')


# --------------------------------------------------------------------------- #
# the repair path, at the handler level
# --------------------------------------------------------------------------- #

def _run(final_text, tokens=(1000, 100)):
    return AgentRun(
        final_text=final_text,
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        model_id="us.anthropic.claude-sonnet-5",
        stop_reason="end_turn",
    )


def test_invalid_json_triggers_exactly_one_repair(monkeypatch, incident):
    from sentry.agent import handler

    responses = [_run("I could not produce JSON."), _run(FINAL_JSON)]
    calls = []

    def fake_run_agent(system_prompt, user_prompt, tools, executor, **kwargs):
        calls.append({"user_prompt": user_prompt, "tools": tools})
        return responses[len(calls) - 1]

    monkeypatch.setattr(handler, "run_agent", fake_run_agent)

    rca, run = handler._run_investigation(incident)

    assert len(calls) == 2, "exactly one repair attempt"
    assert rca.root_cause_category == "code_defect"
    # The repair must not investigate further — no tools, and it is told why.
    assert calls[1]["tools"] == []
    assert "rejected" in calls[1]["user_prompt"]
    assert "I could not produce JSON." in calls[1]["user_prompt"]


def test_repair_folds_cost_into_the_original_run(monkeypatch, incident):
    """Otherwise cost-per-incident silently under-reports every repaired run."""
    from sentry.agent import handler

    responses = [_run("nope", tokens=(1000, 100)), _run(FINAL_JSON, tokens=(2500, 300))]
    calls = []

    def fake_run_agent(system_prompt, user_prompt, tools, executor, **kwargs):
        calls.append(user_prompt)
        return responses[len(calls) - 1]

    monkeypatch.setattr(handler, "run_agent", fake_run_agent)

    _, run = handler._run_investigation(incident)

    assert run.input_tokens == 3500
    assert run.output_tokens == 400


def test_a_second_invalid_response_is_not_repaired_again(monkeypatch, incident):
    """One repair, then give up — an unbounded repair loop is unbounded cost."""
    from sentry.agent import handler
    from sentry.agent.schema import InvalidRCA

    calls = []

    def fake_run_agent(system_prompt, user_prompt, tools, executor, **kwargs):
        calls.append(user_prompt)
        return _run(json.dumps({"root_cause_category": "still_wrong"}))

    monkeypatch.setattr(handler, "run_agent", fake_run_agent)

    with pytest.raises(InvalidRCA):
        handler._run_investigation(incident)

    assert len(calls) == 2


def test_empty_output_escalates_instead_of_repairing(monkeypatch, incident):
    """There is nothing to repair, so spending a second call on it is waste."""
    from sentry.agent import handler

    calls = []

    def fake_run_agent(system_prompt, user_prompt, tools, executor, **kwargs):
        calls.append(user_prompt)
        return _run("   ")

    monkeypatch.setattr(handler, "run_agent", fake_run_agent)

    with pytest.raises(MaxStepsExceeded, match="no output"):
        handler._run_investigation(incident)

    assert len(calls) == 1


def test_valid_first_response_makes_no_second_call(monkeypatch, incident):
    from sentry.agent import handler

    calls = []

    def fake_run_agent(system_prompt, user_prompt, tools, executor, **kwargs):
        calls.append(user_prompt)
        return _run(FINAL_JSON)

    monkeypatch.setattr(handler, "run_agent", fake_run_agent)

    handler._run_investigation(incident)

    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# status routing
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("payload,expected", [
    ({"root_cause_category": "unknown", "needs_human_investigation": True,
      "proposed_remediation": "none"}, "ESCALATED"),
    ({"root_cause_category": "load", "needs_human_investigation": False,
      "proposed_remediation": "none"}, "INFORMATIONAL"),
    ({"root_cause_category": "code_defect", "needs_human_investigation": False,
      "proposed_remediation": "alias_rollback", "suspect_change": "v7"},
     "PENDING_APPROVAL"),
])
def test_route_maps_rca_to_status(payload, expected):
    from sentry.agent.handler import _route
    from sentry.agent.schema import validate

    base = {"summary": "s", "confidence": 0.5, "evidence": ["e"]}
    assert _route(validate({**base, **payload})).value == expected
