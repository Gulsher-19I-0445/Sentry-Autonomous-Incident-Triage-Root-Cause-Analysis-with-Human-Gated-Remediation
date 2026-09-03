"""The approval gate — where a human decides.

The gate authorises; the executor acts. Neither can do both alone, and these
tests are mostly about keeping that separation honest.
"""

import json

import pytest

from sentry.approval import handler as gate
from sentry.common.incidents import IllegalTransition


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setattr(gate, "APPROVAL_TOKEN", "s3cret")
    monkeypatch.setattr(gate, "EXECUTOR_FUNCTION", "sentry-capstone-executor-gulsher")


@pytest.fixture
def invocations(monkeypatch):
    calls = []

    class FakeLambda:
        def invoke(self, **kwargs):
            calls.append(kwargs)
            return {"StatusCode": 202}

    monkeypatch.setattr(gate, "_lambda", FakeLambda())
    return calls


@pytest.fixture
def pending():
    return {
        "incident_id": "api-errors-1788344400",
        "status": "PENDING_APPROVAL",
        "alarm_name": "sentry-capstone-api-errors-gulsher",
        "triggered_at": 1788344400,
        "cost_usd": "0.0599",
        "rca": {
            "root_cause_category": "code_defect",
            "summary": "KeyError after a deploy.",
            "confidence": 0.92,
            "suspect_change": "ac5dec34",
            "affected_component": "sentry-capstone-api-gulsher",
            "proposed_remediation": "alias_rollback",
            "remediation_detail": "Shift the live alias back one version.",
        },
        "trace": {"steps": [], "input_tokens": 14055},
    }


def request(method="GET", path="/incidents", body=None, token="s3cret", actor=None):
    headers = {}
    if token is not None:
        headers["x-approval-token"] = token
    if actor:
        headers["x-actor"] = actor
    return {
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "headers": headers,
        "body": json.dumps(body) if body else None,
    }


def body_of(response):
    return json.loads(response["body"])


# --------------------------------------------------------------------------- #
# authorisation
# --------------------------------------------------------------------------- #

def test_a_request_without_a_token_is_refused():
    assert gate.handler(request(token=None), None)["statusCode"] == 403


def test_a_request_with_the_wrong_token_is_refused():
    assert gate.handler(request(token="guess"), None)["statusCode"] == 403


def test_an_unset_token_refuses_everything(monkeypatch):
    """Fail closed. "No credential configured" must read as "nobody may
    approve", not as "no check required" — the opposite default would turn a
    misconfiguration into an open remediation endpoint."""
    monkeypatch.setattr(gate, "APPROVAL_TOKEN", "")

    assert gate.handler(request(token="anything"), None)["statusCode"] == 403


# --------------------------------------------------------------------------- #
# listing
# --------------------------------------------------------------------------- #

def test_lists_pending_incidents_newest_first(monkeypatch, pending):
    older = {**pending, "incident_id": "older", "triggered_at": 1788000000}
    monkeypatch.setattr(gate, "list_by_status", lambda s, **k: [older, pending])

    response = gate.handler(request(), None)
    listed = body_of(response)

    assert response["statusCode"] == 200
    assert listed["count"] == 2
    assert listed["incidents"][0]["incident_id"] == "api-errors-1788344400"


def test_the_list_carries_what_a_decision_needs(monkeypatch, pending):
    monkeypatch.setattr(gate, "list_by_status", lambda s, **k: [pending])

    entry = body_of(gate.handler(request(), None))["incidents"][0]

    for field in ("summary", "confidence", "suspect_change",
                  "proposed_remediation", "root_cause"):
        assert entry[field] is not None, field


def test_the_list_omits_the_trace(monkeypatch, pending):
    """The trace is large and is fetched per incident, not per list."""
    monkeypatch.setattr(gate, "list_by_status", lambda s, **k: [pending])

    entry = body_of(gate.handler(request(), None))["incidents"][0]

    assert "trace" not in entry


# --------------------------------------------------------------------------- #
# the dashboard's view
# --------------------------------------------------------------------------- #

def test_default_listing_is_the_approval_queue(monkeypatch, pending):
    """The common question is "what needs me?", so that is the default."""
    asked = []
    monkeypatch.setattr(gate, "list_by_status",
                        lambda s, **k: asked.append(s.value) or [pending])

    gate.handler(request(), None)

    assert asked == ["PENDING_APPROVAL"]


def test_status_all_walks_every_status(monkeypatch, pending):
    from sentry.common.incidents import Status

    asked = []
    monkeypatch.setattr(gate, "list_by_status",
                        lambda s, **k: asked.append(s.value) or [])

    event = request(path="/incidents")
    event["queryStringParameters"] = {"status": "all"}
    gate.handler(event, None)

    assert asked == [s.value for s in Status]


def test_a_comma_list_of_statuses_is_supported(monkeypatch):
    """The dashboard groups outcomes — "actioned" is three statuses."""
    asked = []
    monkeypatch.setattr(gate, "list_by_status",
                        lambda s, **k: asked.append(s.value) or [])

    event = request(path="/incidents")
    event["queryStringParameters"] = {"status": "EXECUTED,APPROVED,CLOSED"}
    gate.handler(event, None)

    assert sorted(asked) == ["APPROVED", "CLOSED", "EXECUTED"]


def test_an_unknown_status_is_rejected_with_the_valid_set(monkeypatch):
    monkeypatch.setattr(gate, "list_by_status", lambda s, **k: [])

    event = request(path="/incidents")
    event["queryStringParameters"] = {"status": "MADE_UP"}
    response = gate.handler(event, None)

    assert response["statusCode"] == 400
    assert "PENDING_APPROVAL" in body_of(response)["valid"]


def test_the_listing_reports_counts_per_status(monkeypatch, pending):
    executed = {**pending, "incident_id": "done", "status": "EXECUTED"}
    monkeypatch.setattr(gate, "list_by_status",
                        lambda s, **k: [pending] if s.value == "PENDING_APPROVAL"
                        else ([executed] if s.value == "EXECUTED" else []))

    event = request(path="/incidents")
    event["queryStringParameters"] = {"status": "all"}
    counts = body_of(gate.handler(event, None))["counts_by_status"]

    assert counts == {"PENDING_APPROVAL": 1, "EXECUTED": 1}


def test_the_summary_carries_the_outcome_of_a_finished_incident(monkeypatch, pending):
    """A dashboard showing only a status word cannot tell an operator what was
    actually done, or how to undo it."""
    done = {**pending, "status": "EXECUTED",
            "approved_by": "gulsher",
            "execution_result": {"action": "alias_rollback", "from_version": "3",
                                 "to_version": "2", "undo": "aws lambda ..."}}
    monkeypatch.setattr(gate, "list_by_status", lambda s, **k: [done])

    entry = body_of(gate.handler(request(), None))["incidents"][0]

    assert entry["approved_by"] == "gulsher"
    assert entry["execution_result"]["to_version"] == "2"


def test_the_summary_carries_evidence_and_tool_count(monkeypatch, pending):
    pending["rca"]["evidence"] = ["14 KeyErrors", "invocations flat"]
    pending["trace"]["steps"] = [{"tool": "search_logs"}, {"tool": "get_metrics"}]
    monkeypatch.setattr(gate, "list_by_status", lambda s, **k: [pending])

    entry = body_of(gate.handler(request(), None))["incidents"][0]

    assert len(entry["evidence"]) == 2
    assert entry["tool_calls"] == 2


# --------------------------------------------------------------------------- #
# CORS
# --------------------------------------------------------------------------- #

def test_preflight_needs_no_token():
    """A browser sends OPTIONS with no custom headers, so requiring auth here
    would block the dashboard before it could ever authenticate."""
    response = gate.handler(request("OPTIONS", "/incidents", token=None), None)

    assert response["statusCode"] == 204
    assert response["headers"]["access-control-allow-origin"]


def test_cors_headers_are_on_every_response(monkeypatch, pending):
    monkeypatch.setattr(gate, "list_by_status", lambda s, **k: [pending])

    for response in (gate.handler(request(), None),
                     gate.handler(request(token="wrong"), None),
                     gate.handler(request(path="/nope"), None)):
        assert "access-control-allow-origin" in response["headers"]


def test_preflight_permits_the_headers_the_dashboard_sends():
    allowed = gate.handler(request("OPTIONS", "/incidents", token=None),
                           None)["headers"]["access-control-allow-headers"]

    for header in ("x-approval-token", "x-actor", "content-type"):
        assert header in allowed


def test_fetching_one_incident_returns_the_full_record(monkeypatch, pending):
    """This is the view an operator uses to check the agent's reasoning."""
    monkeypatch.setattr(gate, "get_incident", lambda _id: pending)

    response = gate.handler(
        request(path="/incidents/api-errors-1788344400"), None)

    assert response["statusCode"] == 200
    assert body_of(response)["trace"]["input_tokens"] == 14055


def test_fetching_an_unknown_incident_is_a_404(monkeypatch):
    monkeypatch.setattr(gate, "get_incident", lambda _id: None)

    response = gate.handler(request(path="/incidents/nope"), None)

    assert response["statusCode"] == 404


# --------------------------------------------------------------------------- #
# approving
# --------------------------------------------------------------------------- #

def test_approval_transitions_and_invokes_the_executor(monkeypatch, pending,
                                                       invocations):
    monkeypatch.setattr(gate, "get_incident", lambda _id: pending)
    recorded = {}
    monkeypatch.setattr(gate, "transition",
                        lambda _id, status, **f: recorded.update(
                            {"status": status.value, **f}))

    response = gate.handler(
        request("POST", "/incidents/api-errors-1788344400/approve",
                actor="gulsher"), None)

    assert response["statusCode"] == 202
    assert recorded["status"] == "APPROVED"
    assert recorded["approved_by"] == "gulsher"
    assert len(invocations) == 1
    assert json.loads(invocations[0]["Payload"])["incident_id"] == \
        "api-errors-1788344400"


def test_the_gate_does_not_perform_the_remediation_itself(monkeypatch, pending,
                                                          invocations):
    """The gate authorises, the executor acts. Keeping them separate is what
    makes "who approved" and "what was done" two independent audit facts."""
    monkeypatch.setattr(gate, "get_incident", lambda _id: pending)
    monkeypatch.setattr(gate, "transition", lambda *a, **k: None)

    gate.handler(request("POST", "/incidents/x/approve"), None)

    assert invocations[0]["InvocationType"] == "Event"
    assert invocations[0]["FunctionName"] == "sentry-capstone-executor-gulsher"


def test_approving_twice_conflicts(monkeypatch, pending, invocations):
    """The conditional write in transition() is the guard: two operators
    approving the same incident cannot both reach the executor."""
    monkeypatch.setattr(gate, "get_incident", lambda _id: pending)

    def already_approved(*a, **k):
        raise IllegalTransition("cannot move to APPROVED from APPROVED")

    monkeypatch.setattr(gate, "transition", already_approved)

    response = gate.handler(request("POST", "/incidents/x/approve"), None)

    assert response["statusCode"] == 409
    assert invocations == [], "executor must not run on a conflicting approval"


def test_approving_an_unknown_incident_is_a_404(monkeypatch, invocations):
    monkeypatch.setattr(gate, "get_incident", lambda _id: None)

    response = gate.handler(request("POST", "/incidents/nope/approve"), None)

    assert response["statusCode"] == 404
    assert invocations == []


def test_approval_without_an_executor_configured_says_so(monkeypatch, pending):
    """Better a visible warning than an incident that sits APPROVED forever
    with nobody aware nothing ran."""
    monkeypatch.setattr(gate, "EXECUTOR_FUNCTION", "")
    monkeypatch.setattr(gate, "get_incident", lambda _id: pending)
    monkeypatch.setattr(gate, "transition", lambda *a, **k: None)

    response = gate.handler(request("POST", "/incidents/x/approve"), None)

    assert response["statusCode"] == 200
    assert "warning" in body_of(response)


# --------------------------------------------------------------------------- #
# rejecting
# --------------------------------------------------------------------------- #

def test_rejection_records_the_actor_and_reason(monkeypatch, pending, invocations):
    monkeypatch.setattr(gate, "get_incident", lambda _id: pending)
    recorded = {}
    monkeypatch.setattr(gate, "transition",
                        lambda _id, status, **f: recorded.update(
                            {"status": status.value, **f}))

    response = gate.handler(
        request("POST", "/incidents/x/reject",
                body={"actor": "gulsher", "reason": "wrong commit blamed"}), None)

    assert response["statusCode"] == 200
    assert recorded["status"] == "REJECTED"
    assert recorded["rejection_reason"] == "wrong commit blamed"
    assert invocations == [], "a rejection must never reach the executor"


def test_rejection_without_a_reason_is_allowed(monkeypatch, pending):
    monkeypatch.setattr(gate, "get_incident", lambda _id: pending)
    monkeypatch.setattr(gate, "transition", lambda *a, **k: None)

    assert gate.handler(
        request("POST", "/incidents/x/reject"), None)["statusCode"] == 200


# --------------------------------------------------------------------------- #
# malformed input
# --------------------------------------------------------------------------- #

def test_a_malformed_body_is_a_400():
    event = request("POST", "/incidents/x/approve")
    event["body"] = "{not json"

    assert gate.handler(event, None)["statusCode"] == 400


def test_an_unknown_path_is_a_404():
    assert gate.handler(request(path="/admin/delete-everything"),
                        None)["statusCode"] == 404


def test_every_response_is_json():
    response = gate.handler(request(token=None), None)

    assert response["headers"]["content-type"] == "application/json"
    json.loads(response["body"])
