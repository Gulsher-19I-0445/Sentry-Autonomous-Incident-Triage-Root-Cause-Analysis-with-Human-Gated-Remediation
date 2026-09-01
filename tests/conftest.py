"""Test environment.

Everything in this file exists so the suite runs with NO AWS credentials and NO
network access. Two properties of the codebase force its shape:

  * several modules read env vars at import time (`os.environ["INCIDENTS_TABLE"]`
    in common/incidents.py, `QUEUE_URL` in target_app/api/handler.py), so those
    vars must be set before the first import — a fixture runs too late.
  * every tool module constructs its boto3 client at module level, so a region
    has to be resolvable or construction raises NoRegionError.

Credentials are set to fixed fake values rather than `setdefault`, and AWS_PROFILE
is removed outright. That is deliberate: on a developer machine with real
credentials exported, `setdefault` would leave them in place and a stubbing
mistake could reach the live account. Overriding means the suite *cannot*
authenticate against anything real.
"""

import os
import socket
import sys
from pathlib import Path

# The Lambda zips are built with `src/` as the root, so `src` is what goes on
# the path here too — importing the way Lambda imports is part of the test.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# A profile name would send botocore looking for a real config file.
for _var in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
    os.environ.pop(_var, None)

os.environ.update({
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_REGION": "us-east-1",
    # Stops the credential chain reaching for the instance metadata endpoint.
    "AWS_EC2_METADATA_DISABLED": "true",

    # Read at import time by the modules under test.
    "INCIDENTS_TABLE": "sentry-capstone-incidents-test",
    "WORK_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/000000000000/work-test",
    "QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/000000000000/orders-test",
    "TABLE_NAME": "sentry-capstone-app-test",

    # GitHub is intentionally unconfigured — changes.py must degrade to
    # CloudTrail-only, and that path is asserted in test_tool_payloads.
    "GITHUB_REPO": "",
    "GITHUB_TOKEN_SECRET": "",
})

import pytest  # noqa: E402  (must follow the env setup above)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly on any outbound connection.

    A test that silently reaches AWS would be slow, flaky, and could cost money.
    Constructing a boto3 client opens no socket, so this only trips on a genuine
    call that escaped stubbing.
    """
    def _blocked(*args, **kwargs):
        raise AssertionError(
            "network access attempted during a unit test — stub the client instead"
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)


@pytest.fixture
def incident():
    """A realistic NEW incident, the shape every tool's `run()` receives."""
    return {
        "pk": "INCIDENT#api-errors-1735689600",
        "incident_id": "api-errors-1735689600",
        "status": "NEW",
        "alarm_name": "sentry-capstone-api-errors-gulsher",
        "state_reason": "Threshold Crossed: 3 datapoints were greater than 1.0",
        "metric_name": "Errors",
        "namespace": "AWS/Lambda",
        "dimensions": [{"name": "FunctionName", "value": "sentry-capstone-api-gulsher"}],
        "triggered_at": 1735689600,
        "suppressed_count": 0,
    }


@pytest.fixture
def valid_rca():
    """The minimum RCA that passes every semantic rule in schema.validate."""
    return {
        "root_cause_category": "code_defect",
        "summary": "A KeyError in the order handler began after version 7 was published.",
        "confidence": 0.82,
        "evidence": [
            "14 KeyError: 'customer_tier' entries in the api log group",
            "Invocations flat at ~40/min while errors rose from 0 to 14/min",
        ],
        "suspect_change": "version 7",
        "affected_component": "sentry-capstone-api-gulsher",
        "proposed_remediation": "alias_rollback",
        "remediation_detail": "Shift the live alias back to version 6.",
        "needs_human_investigation": False,
        "runbook_applied": "RB-001",
    }
