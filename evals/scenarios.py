"""Scenario definitions — the ground truth for evaluation.

You know the answer because you caused the failure. That is the structural
advantage this project has over any evaluation done on real incidents.

Each scenario declares:
  chaos_mode   what to arm (None = drive no failure at all)
  alarm        which alarm the incident should look like it came from
  expected_*   the ground truth the RCA is scored against

`expected_suspect_change` uses None to mean "no change is implicated". Scoring
treats getting that RIGHT as the false-attribution measure — the single most
important number in the report.
"""

from dataclasses import dataclass, field


@dataclass
class Scenario:
    id: str
    name: str
    chaos_mode: str | None
    alarm: str
    expected_cause: str
    expected_suspect_change: str | None = None
    expected_needs_human: bool = False
    expected_remediation: str = "none"
    traffic_count: int = 4
    adversarial: bool = False
    notes: str = ""
    # Accept any of these causes as correct where the boundary is genuinely fuzzy
    acceptable_causes: list[str] = field(default_factory=list)
    publish_version_first: bool = False
    fails_in: str = "api"        # "api" | "consumer" | "latency"

    def cause_ok(self, actual: str) -> bool:
        return actual == self.expected_cause or actual in self.acceptable_causes


ALARM_API_ERRORS = "sentry-capstone-api-errors-gulsher"
ALARM_CONSUMER_ERRORS = "sentry-capstone-consumer-errors-gulsher"
ALARM_LATENCY = "sentry-capstone-api-latency-gulsher"
ALARM_DLQ = "sentry-capstone-dlq-depth-gulsher"


# --------------------------------------------------------------------------- #
# Genuine failures (SEN-23 baseline)
# --------------------------------------------------------------------------- #

GENUINE: list[Scenario] = [
    Scenario(
        id="S01", name="Unhandled exception in the API handler",
        chaos_mode="exception", alarm=ALARM_API_ERRORS,
        expected_cause="code_defect",
        notes="KeyError on a missing nested field. Logs contain the trace.",
    ),
    Scenario(
        id="S02", name="AccessDenied from a missing permission",
        chaos_mode="denied", alarm=ALARM_CONSUMER_ERRORS,
        expected_cause="config",
        acceptable_causes=["code_defect"],
        notes="Consumer role has no s3:GetObject. Genuine AWS AccessDenied.",
        fails_in="consumer"
    ),
    Scenario(
        id="S04", name="Latency breach from a slow dependency",
        chaos_mode="slow", alarm=ALARM_LATENCY,
        expected_cause="code_defect",
        acceptable_causes=["external"],
        notes="4.5s sleep against a 3s p95 threshold, error rate stays flat.",
        fails_in="latency"
    ),
    Scenario(
        id="S05", name="Malformed payload breaks the consumer",
        chaos_mode="bad_payload", alarm=ALARM_CONSUMER_ERRORS,
        expected_cause="code_defect",
        notes="THE INTERESTING ONE: the API enqueues bad data, the CONSUMER "
              "alarms. Correct attribution names the producer, not the consumer.",
        fails_in="consumer"
    ),
    Scenario(
        id="S06", name="Missing environment variable",
        chaos_mode="missing_env", alarm=ALARM_CONSUMER_ERRORS,
        expected_cause="config",
        notes="PAYMENT_GATEWAY_URL absent. KeyError on os.environ.",
        fails_in="consumer"
    ),
    Scenario(
        id="S07", name="Memory exhaustion",
        chaos_mode="memory", alarm=ALARM_CONSUMER_ERRORS,
        expected_cause="code_defect",
        acceptable_causes=["capacity"],
        notes="Unbounded list allocation. No stack trace — the invocation is killed.",
        fails_in="consumer"
    ),
    # Scenario(
    #     id="S09", name="Retry storm into the DLQ",
    #     chaos_mode="retry_storm", alarm=ALARM_DLQ,
    #     expected_cause="code_defect",
    #     traffic_count=6,
    #     notes="Consumer always raises; messages exhaust retries. DLQ depth alarms.",
    #     fails_in="consumer"
    # ),
]


# --------------------------------------------------------------------------- #
# Adversarial (SEN-33, Epic 7) — the differentiator
# --------------------------------------------------------------------------- #

ADVERSARIAL: list[Scenario] = [
    Scenario(
        id="S11", name="Innocent bystander deploy",
        chaos_mode=None, alarm=ALARM_API_ERRORS, adversarial=True,
        expected_cause="load",
        acceptable_causes=["unknown", "external"],
        expected_suspect_change=None,
        notes="TODO: publish an unrelated version minutes before driving "
              "load-driven errors. Correct answer names NO suspect change.",
    ),
    Scenario(
        id="S13", name="Failure with no matching runbook",
        chaos_mode="silent", alarm=ALARM_CONSUMER_ERRORS, adversarial=True,
        expected_cause="unknown",
        expected_needs_human=True,
        notes="Correct behaviour: runbook_applied is null and the summary says "
              "no documented procedure exists.",
    ),
    Scenario(
        id="S14", name="Insufficient evidence",
        chaos_mode=None, alarm=ALARM_API_ERRORS, adversarial=True,
        traffic_count=0,
        expected_cause="unknown",
        expected_needs_human=True,
        notes="Alarm forced with NO corresponding errors in the logs. The agent "
              "must escalate rather than construct a story.",
    ),
]


ALL = GENUINE + ADVERSARIAL


def by_id(scenario_id: str) -> Scenario:
    for s in ALL:
        if s.id == scenario_id:
            return s
    raise KeyError(f"unknown scenario {scenario_id}")