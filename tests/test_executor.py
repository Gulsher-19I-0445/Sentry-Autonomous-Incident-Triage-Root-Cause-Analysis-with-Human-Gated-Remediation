"""The executor — the only component that changes anything.

Everything here is about refusing to act. The agent that produces the RCA is a
language model, so every field the executor reads is untrusted input: a
free-text component name, a prose remediation detail, a confidence score that
means nothing to an AWS API call. The tests are mostly about what happens when
those fields are wrong.
"""

import pytest

from sentry.executor import handler as ex


@pytest.fixture
def approved_incident():
    return {
        "incident_id": "api-errors-1788344400",
        "status": "APPROVED",
        "rca": {
            "root_cause_category": "code_defect",
            "summary": "KeyError after a deploy.",
            "confidence": 0.92,
            "affected_component": "sentry-capstone-api-gulsher",
            "proposed_remediation": "alias_rollback",
            "suspect_change": "version 3",
        },
    }


@pytest.fixture
def fake_lambda(monkeypatch):
    """Stub the Lambda client, recording mutations."""
    state = {"alias_version": "3", "updates": [],
             "versions": ["1", "2", "3"]}

    class FakePaginator:
        def paginate(self, FunctionName):
            return [{"Versions": [{"Version": "$LATEST"}]
                     + [{"Version": v} for v in state["versions"]]}]

    class FakeLambda:
        def get_paginator(self, name):
            return FakePaginator()

        def get_alias(self, FunctionName, Name):
            return {"FunctionVersion": state["alias_version"]}

        def update_alias(self, FunctionName, Name, FunctionVersion):
            state["updates"].append((FunctionName, Name, FunctionVersion))
            state["alias_version"] = FunctionVersion

        def invoke(self, **kwargs):
            state.setdefault("invokes", []).append(kwargs)
            return {}

    monkeypatch.setattr(ex, "_lambda", FakeLambda())
    return state


# --------------------------------------------------------------------------- #
# the status guard
# --------------------------------------------------------------------------- #

def test_execution_requires_approval(monkeypatch, approved_incident):
    """The whole safety argument rests on this: nothing runs unapproved."""
    for status in ("NEW", "INVESTIGATING", "PENDING_APPROVAL", "ESCALATED",
                   "REJECTED", "EXECUTED"):
        approved_incident["status"] = status
        monkeypatch.setattr(ex, "get_incident", lambda _id, i=approved_incident: i)
        called = []
        monkeypatch.setattr(ex, "transition",
                            lambda *a, **k: called.append(a))

        result = ex.execute("api-errors-1")

        assert result["skipped"] is True, f"executed from {status}"
        assert called == [], f"transitioned from {status}"


def test_a_redelivery_after_execution_is_not_an_error(monkeypatch, approved_incident):
    """SQS redelivery and a double-clicked approve button land here. The first
    execution already happened; this must be quiet, not a failure."""
    approved_incident["status"] = "EXECUTED"
    monkeypatch.setattr(ex, "get_incident", lambda _id: approved_incident)

    result = ex.execute("api-errors-1")

    assert result == {"skipped": True, "status": "EXECUTED"}


def test_missing_incident_raises(monkeypatch):
    monkeypatch.setattr(ex, "get_incident", lambda _id: None)

    with pytest.raises(ex.ExecutionError, match="not found"):
        ex.execute("does-not-exist")


# --------------------------------------------------------------------------- #
# the allow-list
# --------------------------------------------------------------------------- #

def test_resolves_an_allow_listed_function():
    assert ex._resolve_function("sentry-capstone-api-gulsher") == \
        "sentry-capstone-api-gulsher"


def test_resolves_a_component_named_loosely():
    """The RCA field is model-written prose, not an ARN."""
    assert ex._resolve_function("the api lambda (sentry-capstone-api-gulsher)") == \
        "sentry-capstone-api-gulsher"


def test_refuses_a_function_outside_the_allow_list():
    """An RCA naming another team's function must not be actionable — this is
    a shared account, and the agent's output is untrusted input."""
    with pytest.raises(ex.ExecutionError, match="refusing to act"):
        ex._resolve_function("some-other-team-payments-prod")


def test_refuses_when_no_component_was_named():
    with pytest.raises(ex.ExecutionError, match="nothing to roll back"):
        ex._resolve_function(None)


# --------------------------------------------------------------------------- #
# choosing the rollback target
# --------------------------------------------------------------------------- #

def test_rolls_back_to_the_previous_published_version(fake_lambda):
    assert ex._previous_version("sentry-capstone-api-gulsher", "3") == "2"


def test_version_ordering_is_numeric_not_lexical(fake_lambda):
    """Lambda versions are strings; sorted() would put "10" before "9"."""
    fake_lambda["versions"] = ["8", "9", "10", "11"]

    assert ex._previous_version("sentry-capstone-api-gulsher", "10") == "9"


def test_refuses_to_roll_back_past_the_earliest_version(fake_lambda):
    with pytest.raises(ex.ExecutionError, match="nothing to roll back to"):
        ex._previous_version("sentry-capstone-api-gulsher", "1")


def test_refuses_when_the_alias_points_somewhere_unpublished(fake_lambda):
    with pytest.raises(ex.ExecutionError, match="not in the published list"):
        ex._previous_version("sentry-capstone-api-gulsher", "99")


def test_rollback_target_ignores_the_rcas_suspect_change(monkeypatch, fake_lambda,
                                                         approved_incident):
    """suspect_change is prose — "version 7", a sha, a CloudTrail event name.
    Parsing it into a version number to hand to an AWS mutation would be
    turning model output directly into an action. What the alias points at is
    a fact, so the target is derived from that instead."""
    approved_incident["rca"]["suspect_change"] = "commit deadbeef, probably v99"
    monkeypatch.setattr(ex, "get_incident", lambda _id: approved_incident)
    monkeypatch.setattr(ex, "transition", lambda *a, **k: None)

    result = ex.execute("api-errors-1")

    assert result["to_version"] == "2"      # from the alias, not the prose
    assert fake_lambda["updates"] == [("sentry-capstone-api-gulsher", "live", "2")]


def test_rollback_records_how_to_undo_it(monkeypatch, fake_lambda, approved_incident):
    """An operator undoing this at 3am should not have to reconstruct which
    version was live before."""
    monkeypatch.setattr(ex, "get_incident", lambda _id: approved_incident)
    monkeypatch.setattr(ex, "transition", lambda *a, **k: None)

    result = ex.execute("api-errors-1")

    assert result["from_version"] == "3"
    assert "--function-version 3" in result["undo"]


def test_execution_result_is_recorded_on_the_incident(monkeypatch, fake_lambda,
                                                      approved_incident):
    monkeypatch.setattr(ex, "get_incident", lambda _id: approved_incident)
    recorded = {}
    monkeypatch.setattr(ex, "transition",
                        lambda _id, status, **fields: recorded.update(fields))

    ex.execute("api-errors-1")

    assert recorded["execution_result"]["action"] == "alias_rollback"


# --------------------------------------------------------------------------- #
# remediation dispatch
# --------------------------------------------------------------------------- #

def test_remediation_none_executes_nothing(monkeypatch, approved_incident):
    """INFORMATIONAL incidents reach EXECUTED without any action."""
    approved_incident["rca"]["proposed_remediation"] = "none"
    monkeypatch.setattr(ex, "get_incident", lambda _id: approved_incident)
    statuses = []
    monkeypatch.setattr(ex, "transition",
                        lambda _id, status, **k: statuses.append(status.value))

    result = ex.execute("api-errors-1")

    assert result == {"action": "none"}
    assert statuses == ["EXECUTED"]


def test_unknown_remediation_is_refused(monkeypatch, approved_incident):
    """A model that invents a remediation must not reach an AWS call."""
    approved_incident["rca"]["proposed_remediation"] = "restart_the_database"
    monkeypatch.setattr(ex, "get_incident", lambda _id: approved_incident)

    with pytest.raises(ex.UnsupportedRemediation, match="restart_the_database"):
        ex.execute("api-errors-1")


def test_every_schema_remediation_is_handled_or_explicitly_inert():
    """If the schema gains a remediation the executor does not implement, that
    is a gap discovered here rather than at approval time."""
    from sentry.agent.schema import VALID_REMEDIATIONS

    for remediation in VALID_REMEDIATIONS:
        assert remediation == "none" or remediation in ex.ACTIONS, remediation


# --------------------------------------------------------------------------- #
# feature flags
# --------------------------------------------------------------------------- #

@pytest.fixture
def fake_table(monkeypatch):
    state = {"flags": ["chaos_exception", "chaos_slow"], "updates": []}

    class FakeTable:
        def scan(self, **kwargs):
            return {"Items": [{"pk": f"FLAG#{f}"} for f in state["flags"]]}

        def update_item(self, Key, **kwargs):
            state["updates"].append(Key["pk"])

    class FakeDDB:
        def Table(self, name):
            return FakeTable()

    monkeypatch.setattr(ex, "_ddb", FakeDDB())
    monkeypatch.setattr(ex, "APP_TABLE", "sentry-capstone-app-gulsher")
    return state


def test_flag_remediation_disables_the_named_flag(fake_table, approved_incident):
    approved_incident["rca"]["remediation_detail"] = \
        "disable the chaos_slow flag in the app table"

    result = ex._disable_flag(approved_incident, approved_incident["rca"])

    assert result["flag"] == "chaos_slow"
    assert result["set_to"] == "disabled"
    assert fake_table["updates"] == ["FLAG#chaos_slow"]


def test_flag_remediation_refuses_an_ambiguous_detail(fake_table, approved_incident):
    """Naming two flags is not a reason to pick one."""
    approved_incident["rca"]["remediation_detail"] = \
        "turn off chaos_slow and chaos_exception"

    with pytest.raises(ex.ExecutionError, match="more than one flag"):
        ex._disable_flag(approved_incident, approved_incident["rca"])


def test_flag_remediation_refuses_a_flag_that_does_not_exist(fake_table,
                                                             approved_incident):
    """Silently doing nothing while reporting EXECUTED is worse than failing."""
    approved_incident["rca"]["remediation_detail"] = "disable the payments flag"

    with pytest.raises(ex.ExecutionError, match="no enabled flag named"):
        ex._disable_flag(approved_incident, approved_incident["rca"])


def test_flag_remediation_requires_a_detail(fake_table, approved_incident):
    approved_incident["rca"]["remediation_detail"] = None

    with pytest.raises(ex.ExecutionError, match="no .*remediation_detail"):
        ex._disable_flag(approved_incident, approved_incident["rca"])


def test_flag_remediation_needs_the_table_configured(monkeypatch, approved_incident):
    monkeypatch.setattr(ex, "APP_TABLE", "")
    approved_incident["rca"]["remediation_detail"] = "disable chaos_slow"

    with pytest.raises(ex.ExecutionError, match="APP_TABLE"):
        ex._disable_flag(approved_incident, approved_incident["rca"])


# --------------------------------------------------------------------------- #
# failure handling
# --------------------------------------------------------------------------- #

def test_an_aws_failure_becomes_an_execution_error(monkeypatch, approved_incident):
    """A boto exception must not escape as itself — the incident needs to move
    to FAILED with a reason, not crash the invocation opaquely."""
    monkeypatch.setattr(ex, "get_incident", lambda _id: approved_incident)

    def exploding(incident, rca):
        raise RuntimeError("AccessDeniedException")

    monkeypatch.setitem(ex.ACTIONS, "alias_rollback", exploding)

    with pytest.raises(ex.ExecutionError, match="AccessDeniedException"):
        ex.execute("api-errors-1")


def test_handler_records_the_failure_on_the_incident(monkeypatch, approved_incident):
    monkeypatch.setattr(ex, "get_incident", lambda _id: approved_incident)
    recorded = {}

    def fake_transition(_id, status, **fields):
        recorded["status"] = status.value
        recorded.update(fields)

    monkeypatch.setattr(ex, "transition", fake_transition)
    monkeypatch.setitem(ex.ACTIONS, "alias_rollback",
                        lambda i, r: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(ex.ExecutionError):
        ex.handler({"incident_id": "api-errors-1"}, None)

    assert recorded["status"] == "FAILED"
    assert "boom" in recorded["failure_reason"]


def test_handler_rejects_an_event_with_no_incident_id():
    with pytest.raises(ValueError, match="incident_id"):
        ex.handler({}, None)
