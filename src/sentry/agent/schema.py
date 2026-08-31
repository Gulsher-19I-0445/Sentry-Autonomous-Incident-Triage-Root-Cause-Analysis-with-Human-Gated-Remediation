"""The RCA schema — the contract every other component depends on.

Deliberately validated with the stdlib rather than pydantic: pydantic-core ships
as a compiled wheel, so bundling it for Lambda from Windows means fighting
--platform flags for the right architecture. Not worth it for one model.

Design notes that matter:
  * suspect_change is NULLABLE and must stay that way. "No deploy caused this"
    is a first-class answer, and it is what the innocent-bystander scenario tests.
  * needs_human_investigation is an OUTPUT, not a fallback. Abstention is a
    correct result, scored as such in the eval harness.
  * confidence is scored against correctness later (calibration). A model that
    is wrong AND uncertain is workable; wrong and certain is dangerous.
"""

from dataclasses import asdict, dataclass, field
from enum import StrEnum


class RootCause(StrEnum):
    CODE_DEFECT = "code_defect"      # a change to application code
    CONFIG = "config"                # env var, IAM, setting
    CAPACITY = "capacity"            # throttling, concurrency, memory limits
    LOAD = "load"                    # traffic — not a defect
    EXTERNAL = "external"            # downstream dependency
    UNKNOWN = "unknown"              # insufficient evidence; must escalate


class Remediation(StrEnum):
    ALIAS_ROLLBACK = "alias_rollback"
    FEATURE_FLAG = "feature_flag"
    NONE = "none"                    # nothing safe or applicable to automate


VALID_CAUSES = {c.value for c in RootCause}
VALID_REMEDIATIONS = {r.value for r in Remediation}


class InvalidRCA(ValueError):
    """Raised with a message intended to be fed back to the model for repair."""


@dataclass
class RCA:
    root_cause_category: str
    summary: str
    confidence: float
    evidence: list[str] = field(default_factory=list)
    suspect_change: str | None = None          # commit sha, version, or null
    affected_component: str | None = None
    proposed_remediation: str = Remediation.NONE.value
    remediation_detail: str | None = None
    needs_human_investigation: bool = False
    runbook_applied: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def validate(raw: dict) -> RCA:
    """Validate a model response into an RCA, or raise InvalidRCA with a
    message specific enough for the model to repair itself."""
    errors: list[str] = []

    cause = raw.get("root_cause_category")
    if cause not in VALID_CAUSES:
        errors.append(
            f"root_cause_category must be one of {sorted(VALID_CAUSES)}, got {cause!r}"
        )

    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("summary must be a non-empty string")

    confidence = raw.get("confidence")
    if not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
        errors.append(f"confidence must be a number between 0 and 1, got {confidence!r}")

    evidence = raw.get("evidence", [])
    if not isinstance(evidence, list) or not all(isinstance(e, str) for e in evidence):
        errors.append("evidence must be a list of strings")
    elif not evidence:
        errors.append("evidence must contain at least one item — never conclude without it")

    remediation = raw.get("proposed_remediation", Remediation.NONE.value)
    if remediation not in VALID_REMEDIATIONS:
        errors.append(
            f"proposed_remediation must be one of {sorted(VALID_REMEDIATIONS)}, got {remediation!r}"
        )

    needs_human = raw.get("needs_human_investigation", False)
    if not isinstance(needs_human, bool):
        errors.append("needs_human_investigation must be true or false")

    if cause == RootCause.UNKNOWN.value and not needs_human:
        errors.append(
            "root_cause_category 'unknown' requires needs_human_investigation = true"
        )

    if cause in (RootCause.LOAD.value, RootCause.EXTERNAL.value) and \
            remediation != Remediation.NONE.value:
        errors.append(
            f"a '{cause}' cause is not a code defect, so proposed_remediation must be 'none'"
        )

    if remediation == Remediation.ALIAS_ROLLBACK.value and not raw.get("suspect_change"):
        errors.append(
            "alias_rollback requires a suspect_change — never roll back without "
            "identifying which change is at fault"
        )

    if needs_human and remediation != Remediation.NONE.value:
        errors.append(
            "if human investigation is needed, do not also propose an automated remediation"
        )

    if errors:
        raise InvalidRCA("; ".join(errors))

    return RCA(
        root_cause_category=cause,
        summary=summary.strip(),
        confidence=float(confidence),
        evidence=evidence,
        suspect_change=raw.get("suspect_change"),
        affected_component=raw.get("affected_component"),
        proposed_remediation=remediation,
        remediation_detail=raw.get("remediation_detail"),
        needs_human_investigation=needs_human,
        runbook_applied=raw.get("runbook_applied"),
    )


# Included verbatim in the system prompt so the model knows the exact shape.
SCHEMA_DESCRIPTION = """Respond with a single JSON object, no prose around it:

{
  "root_cause_category": "code_defect|config|capacity|load|external|unknown",
  "summary": "one or two sentences explaining what happened and why",
  "confidence": 0.0-1.0,
  "evidence": ["specific observations that support this conclusion"],
  "suspect_change": "version/commit that caused it, or null if none did",
  "affected_component": "which function or resource failed",
  "proposed_remediation": "alias_rollback|feature_flag|none",
  "remediation_detail": "what exactly to do, or null",
  "needs_human_investigation": true|false,
  "runbook_applied": "runbook id if one matched, or null"
}

Rules:
- Never conclude without evidence. If the evidence is insufficient, use
  "unknown", set needs_human_investigation to true, and explain what is missing.
- A deploy happening near the incident is NOT by itself evidence that it caused
  the incident. Say so, and set suspect_change to null, unless the logs or
  metrics actually connect the change to the failure.
- If the cause is load or an external dependency, it is not a code defect:
  proposed_remediation must be "none".
- Only propose alias_rollback when you have identified the specific change at fault.
- If no runbook covers this failure, set runbook_applied to null and say so in
  the summary rather than inventing guidance."""