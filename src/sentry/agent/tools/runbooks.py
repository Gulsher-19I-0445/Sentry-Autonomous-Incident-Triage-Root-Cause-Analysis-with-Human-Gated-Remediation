"""SEN-19 — runbook tool.

Deliberately NOT a vector store. The corpus is a handful of pages, and
OpenSearch Serverless minimum capacity billing would cost more per month than
every other line item in this project combined. Keyword matching over
prompt-resident text is the right engineering call at this size, and saying so
is a better answer than quietly over-building.

The important behaviour is the NEGATIVE case. When no runbook matches, this
tool must say so clearly, so the agent reports "no documented procedure covers
this" instead of inventing plausible-sounding guidance. That is the
no-runbook decoy scenario, and it is scored.
"""

import re

from ...common.logging import get_logger, log_event
from .base import truncate

logger = get_logger("tool.runbooks")

MIN_SCORE = 2          # below this, treat as no match rather than a weak one
MAX_RESULTS = 3


# Deliberately incomplete: some failure modes have NO runbook, and the agent
# must handle that correctly rather than stretching a near-miss to fit.
RUNBOOKS: list[dict] = [
    {
        "id": "RB-001",
        "title": "Unhandled exception after a deployment",
        "keywords": ["keyerror", "typeerror", "attributeerror", "unhandled",
                     "exception", "traceback", "stack trace", "deploy", "500"],
        "symptoms": "Error rate rises sharply immediately after a new version is published.",
        "procedure": (
            "1. Identify the version published closest before the first error.\n"
            "2. Confirm the stack trace points at code changed in that version.\n"
            "3. If confirmed, roll the alias back to the previous version.\n"
            "4. Do NOT roll back if the trace is unrelated to the changed files."
        ),
        "remediation": "alias_rollback",
    },
    {
        "id": "RB-002",
        "title": "AccessDenied from a permission change",
        "keywords": ["accessdenied", "access denied", "not authorized",
                     "unauthorized", "forbidden", "iam", "permission", "policy",
                     "clienterror"],
        "symptoms": "Calls to an AWS service fail with AccessDenied where they previously succeeded.",
        "procedure": (
            "1. Identify which principal and which action was denied.\n"
            "2. Check CloudTrail for PutRolePolicy or AttachRolePolicy events on that role.\n"
            "3. If a policy change removed the permission, restore it.\n"
            "4. If the permission was never granted, this is a code or config "
            "defect, not a regression — escalate."
        ),
        "remediation": "none",
    },
    {
        "id": "RB-003",
        "title": "DynamoDB throttling",
        "keywords": ["throttl", "provisionedthroughput", "capacity",
                     "provisioned", "rate exceeded", "dynamodb"],
        "symptoms": "Writes or reads fail intermittently; throttle metrics rise with traffic.",
        "procedure": (
            "1. Compare the throttle series against invocations for the same window.\n"
            "2. If throttles track a traffic rise, this is capacity, not a defect.\n"
            "3. Capacity changes are not automated here — escalate with the numbers."
        ),
        "remediation": "none",
    },
    {
        "id": "RB-004",
        "title": "Messages accumulating in the dead letter queue",
        "keywords": ["dlq", "dead letter", "dead-letter", "redrive",
                     "batchitemfailures", "receive count", "message"],
        "symptoms": "DLQ depth above zero; the consumer reports repeated failures for the same message.",
        "procedure": (
            "1. Inspect a sample message to determine whether the payload is malformed.\n"
            "2. A malformed payload usually means the PRODUCER is at fault, not the consumer.\n"
            "3. Trace the correlation id back to the producing service before blaming the consumer."
        ),
        "remediation": "none",
    },
    {
        "id": "RB-005",
        "title": "Lambda memory exhaustion",
        "keywords": ["memory", "oom", "out of memory", "max memory used",
                     "memorysize", "killed"],
        "symptoms": "Invocations end without a normal completion; Max Memory Used approaches the configured limit.",
        "procedure": (
            "1. Check whether Max Memory Used reached the configured MemorySize.\n"
            "2. If a recent change introduced unbounded accumulation, roll it back.\n"
            "3. Otherwise raising the memory limit is a mitigation, not a fix — escalate."
        ),
        "remediation": "alias_rollback",
    },
    {
        "id": "RB-006",
        "title": "Elevated latency without errors",
        "keywords": ["latency", "duration", "slow", "timeout", "p95", "timed out"],
        "symptoms": "Duration p95 breaches its threshold while the error rate stays flat.",
        "procedure": (
            "1. Establish whether latency rose in step with invocations (load) or independently.\n"
            "2. Check for a recent change touching downstream calls.\n"
            "3. Load-driven latency is not a defect — report it without proposing a code fix."
        ),
        "remediation": "none",
    },
]


TOOL_SPEC = {
    "toolSpec": {
        "name": "search_runbooks",
        "description": (
            "Search the team's operational runbooks for a documented procedure "
            "matching the failure you observed. Returns matching runbooks with "
            "their symptoms and steps, or an explicit no-match result. If nothing "
            "matches, say so in your analysis — do not invent a procedure that is "
            "not documented."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "symptoms": {
                        "type": "string",
                        "description": (
                            "Describe what you observed — error types, messages, "
                            "metric behaviour. Free text; the more specific the better."
                        ),
                    },
                },
                "required": ["symptoms"],
            }
        },
    }
}


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _score(runbook: dict, symptoms: str) -> int:
    """Count keyword hits. Substring match so 'throttl' catches 'throttling'."""
    lowered = (symptoms or "").lower()
    tokens = _tokenize(symptoms)
    score = 0
    for keyword in runbook["keywords"]:
        if " " in keyword or len(keyword) > 6:
            if keyword in lowered:
                score += 2          # phrase and long-stem matches are stronger signal
        elif keyword in tokens:
            score += 1
    return score


def run(incident: dict, symptoms: str = "") -> dict:
    if not symptoms.strip():
        return {
            "result_count": 0,
            "matched": False,
            "message": "No symptoms provided. Describe what you observed first.",
        }

    scored = [(_score(rb, symptoms), rb) for rb in RUNBOOKS]
    hits = sorted(
        [(s, rb) for s, rb in scored if s >= MIN_SCORE],
        key=lambda pair: pair[0],
        reverse=True,
    )[:MAX_RESULTS]

    log_event(logger, "info", "runbook search",
              symptoms=truncate(symptoms, 120),
              matched=len(hits),
              best_score=hits[0][0] if hits else 0)

    if not hits:
        # The negative case matters as much as the positive one.
        return {
            "result_count": 0,
            "matched": False,
            "runbooks_available": len(RUNBOOKS),
            "message": (
                "No runbook matches these symptoms. There is no documented "
                "procedure for this failure. Report that in your analysis and set "
                "runbook_applied to null rather than proposing undocumented steps."
            ),
        }

    return {
        "result_count": len(hits),
        "matched": True,
        "runbooks": [
            {
                "id": rb["id"],
                "title": rb["title"],
                "match_score": score,
                "symptoms": rb["symptoms"],
                "procedure": rb["procedure"],
                "suggested_remediation": rb["remediation"],
            }
            for score, rb in hits
        ],
    }