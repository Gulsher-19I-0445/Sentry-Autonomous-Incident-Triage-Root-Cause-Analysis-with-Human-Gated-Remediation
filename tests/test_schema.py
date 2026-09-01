"""RCA validation.

Two things are being tested, and the second matters more than the first:

  1. a well-formed RCA validates
  2. every rejection message names the field and says what is wrong

(2) is load-bearing. The message is fed straight back to the model as the repair
prompt, so "invalid input" costs a second full Converse round trip and probably
fails again. "confidence must be a number between 0 and 1, got 95" repairs on
the first attempt.
"""

import pytest

from sentry.agent.schema import (
    SCHEMA_DESCRIPTION,
    InvalidRCA,
    RCA,
    Remediation,
    RootCause,
    VALID_CAUSES,
    VALID_REMEDIATIONS,
    validate,
)


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #

def test_valid_rca_passes(valid_rca):
    rca = validate(valid_rca)

    assert isinstance(rca, RCA)
    assert rca.root_cause_category == "code_defect"
    assert rca.confidence == 0.82
    assert rca.suspect_change == "version 7"
    assert rca.proposed_remediation == "alias_rollback"
    assert len(rca.evidence) == 2


def test_optional_fields_default_without_error():
    """The model routinely omits nullable fields entirely rather than sending
    null. That must not be a validation failure."""
    rca = validate({
        "root_cause_category": "external",
        "summary": "A downstream payment gateway returned 503s.",
        "confidence": 0.6,
        "evidence": ["12 consecutive 503 responses from the gateway"],
    })

    assert rca.suspect_change is None
    assert rca.affected_component is None
    assert rca.runbook_applied is None
    assert rca.proposed_remediation == "none"
    assert rca.needs_human_investigation is False


def test_summary_is_stripped(valid_rca):
    valid_rca["summary"] = "  padded on both sides  "
    assert validate(valid_rca).summary == "padded on both sides"


def test_integer_confidence_is_accepted(valid_rca):
    """The model emits a bare 1 or 0 often enough to matter."""
    valid_rca["confidence"] = 1
    assert validate(valid_rca).confidence == 1.0


def test_to_dict_round_trips(valid_rca):
    """to_dict() output is written to DynamoDB and re-validated by the harness."""
    once = validate(valid_rca).to_dict()
    assert validate(once).to_dict() == once


# --------------------------------------------------------------------------- #
# abstention — a correct output, not a failure mode
# --------------------------------------------------------------------------- #

def test_abstention_is_valid():
    rca = validate({
        "root_cause_category": "unknown",
        "summary": "Errors are visible but nothing explains them.",
        "confidence": 0.2,
        "evidence": ["9 timeouts in the consumer log group"],
        "needs_human_investigation": True,
        "proposed_remediation": "none",
    })

    assert rca.root_cause_category == RootCause.UNKNOWN.value
    assert rca.needs_human_investigation is True


def test_suspect_change_may_be_null_with_a_real_cause():
    """The innocent-bystander scenario: a genuine defect with no deploy to blame."""
    rca = validate({
        "root_cause_category": "code_defect",
        "summary": "A latent null dereference triggered by an unusual payload.",
        "confidence": 0.55,
        "evidence": ["TypeError in the consumer for order 91d2"],
        "suspect_change": None,
        "proposed_remediation": "none",
    })

    assert rca.suspect_change is None


# --------------------------------------------------------------------------- #
# each semantic rule, and the specificity of its message
# --------------------------------------------------------------------------- #

def _reject(payload) -> str:
    with pytest.raises(InvalidRCA) as exc:
        validate(payload)
    return str(exc.value)


def test_rejects_unknown_root_cause(valid_rca):
    valid_rca["root_cause_category"] = "gremlins"
    message = _reject(valid_rca)

    assert "root_cause_category" in message
    assert "gremlins" in message
    # The valid options are listed, so the repair does not have to guess.
    for cause in VALID_CAUSES:
        assert cause in message


def test_rejects_missing_root_cause(valid_rca):
    del valid_rca["root_cause_category"]
    assert "root_cause_category" in _reject(valid_rca)


@pytest.mark.parametrize("bad", ["", "   ", None, 42, ["a"]])
def test_rejects_empty_or_non_string_summary(valid_rca, bad):
    valid_rca["summary"] = bad
    assert "summary" in _reject(valid_rca)


@pytest.mark.parametrize("bad", [-0.1, 1.1, 95, "high", None])
def test_rejects_out_of_range_confidence(valid_rca, bad):
    valid_rca["confidence"] = bad
    message = _reject(valid_rca)

    assert "confidence" in message
    assert "between 0 and 1" in message
    assert repr(bad) in message, "the offending value must appear in the message"


def test_rejects_empty_evidence(valid_rca):
    """'Never conclude without evidence' is the rule the whole project rests on."""
    valid_rca["evidence"] = []
    message = _reject(valid_rca)

    assert "evidence" in message
    assert "at least one" in message


@pytest.mark.parametrize("bad", ["a string", [1, 2], [{"x": 1}], None])
def test_rejects_malformed_evidence(valid_rca, bad):
    valid_rca["evidence"] = bad
    assert "evidence must be a list of strings" in _reject(valid_rca)


def test_rejects_unknown_remediation(valid_rca):
    valid_rca["proposed_remediation"] = "restart_everything"
    message = _reject(valid_rca)

    assert "proposed_remediation" in message
    for remediation in VALID_REMEDIATIONS:
        assert remediation in message


def test_rejects_non_boolean_needs_human(valid_rca):
    valid_rca["needs_human_investigation"] = "yes"
    assert "needs_human_investigation" in _reject(valid_rca)


def test_unknown_cause_requires_escalation(valid_rca):
    """'unknown' without escalation is an incident that silently goes nowhere."""
    valid_rca.update({
        "root_cause_category": "unknown",
        "needs_human_investigation": False,
        "proposed_remediation": "none",
        "suspect_change": None,
    })
    message = _reject(valid_rca)

    assert "unknown" in message
    assert "needs_human_investigation" in message


@pytest.mark.parametrize("cause", ["load", "external"])
def test_non_defect_cause_forbids_remediation(valid_rca, cause):
    """The load-spike decoy: correctly identifying load and then proposing a
    rollback anyway is the failure this rule exists to block."""
    valid_rca["root_cause_category"] = cause
    valid_rca["proposed_remediation"] = "alias_rollback"
    message = _reject(valid_rca)

    assert cause in message
    assert "none" in message


def test_alias_rollback_requires_a_suspect_change(valid_rca):
    """Rolling back without naming what is at fault is the dangerous action this
    schema exists to prevent."""
    valid_rca["suspect_change"] = None
    message = _reject(valid_rca)

    assert "alias_rollback" in message
    assert "suspect_change" in message


@pytest.mark.parametrize("empty", [None, "", []])
def test_alias_rollback_rejects_falsy_suspect_change(valid_rca, empty):
    valid_rca["suspect_change"] = empty
    assert "suspect_change" in _reject(valid_rca)


def test_escalation_and_remediation_are_mutually_exclusive(valid_rca):
    """Either a human decides or the agent proposes a fix — never both, or the
    approval queue fills with items nobody owns."""
    valid_rca["needs_human_investigation"] = True
    message = _reject(valid_rca)

    assert "human investigation" in message


def test_multiple_errors_are_reported_together():
    """One repair round trip, not four. Each round trip is a full Converse call."""
    message = _reject({
        "root_cause_category": "nonsense",
        "summary": "",
        "confidence": 9,
        "evidence": [],
    })

    assert message.count(";") >= 3
    for field in ("root_cause_category", "summary", "confidence", "evidence"):
        assert field in message


# --------------------------------------------------------------------------- #
# the schema and the prompt must not drift apart
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cause", sorted(VALID_CAUSES))
def test_prompt_documents_every_root_cause(cause):
    """SCHEMA_DESCRIPTION is pasted into the system prompt. An enum value the
    prompt never mentions is one the model will never emit."""
    assert cause in SCHEMA_DESCRIPTION


@pytest.mark.parametrize("remediation", sorted(VALID_REMEDIATIONS))
def test_prompt_documents_every_remediation(remediation):
    assert remediation in SCHEMA_DESCRIPTION


@pytest.mark.parametrize("field", [f.name for f in RCA.__dataclass_fields__.values()])
def test_prompt_documents_every_field(field):
    assert field in SCHEMA_DESCRIPTION, (
        f"{field} is in the RCA dataclass but absent from the prompt schema"
    )


def test_system_prompt_embeds_the_schema():
    from sentry.agent.prompt import SYSTEM_PROMPT

    assert SCHEMA_DESCRIPTION in SYSTEM_PROMPT


def test_system_prompt_grants_permission_to_abstain():
    """The abstention axis depends on the prompt explicitly allowing 'unknown'.
    A well-meaning prompt edit that drops this quietly zeroes that score."""
    from sentry.agent.prompt import SYSTEM_PROMPT

    assert "unknown" in SYSTEM_PROMPT
    assert "needs_human_investigation" in SYSTEM_PROMPT
    assert "Correlation is not causation" in SYSTEM_PROMPT


def test_enums_and_validation_sets_agree():
    assert VALID_CAUSES == {c.value for c in RootCause}
    assert VALID_REMEDIATIONS == {r.value for r in Remediation}
