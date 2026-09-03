"""The scenario definitions are ground truth — check them like code.

A wrong expectation here does not crash anything. It quietly produces a wrong
score, which is worse: the number still looks like a result. S13 declared a
consumer-side fault but inherited the default `fails_in="api"`, so the harness
looked in the wrong place, found nothing, and reported "did not reproduce" —
for a scenario that was working correctly the whole time.
"""

import pytest

import scenarios as sc
from target_app.common._internal import MODES


ALL = sc.ALL


def test_scenario_ids_are_unique():
    ids = [s.id for s in ALL]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.id)
def test_chaos_mode_exists(scenario):
    """A typo in a mode name arms nothing and reproduces as silence."""
    if scenario.chaos_mode is not None:
        assert scenario.chaos_mode in MODES, \
            f"{scenario.id} arms {scenario.chaos_mode!r}, which is not a known mode"


# Modes that deliberately fail somewhere other than where they are armed.
# bad_payload arms in the API and corrupts the message the API enqueues, so the
# CONSUMER is what breaks — that mismatch is the scenario, not a mistake. It is
# the one case where the alarm fires on a component that is not at fault.
CROSS_COMPONENT = {"bad_payload": "consumer"}


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.id)
def test_fails_in_matches_where_the_failure_surfaces(scenario):
    """The bug that disabled S13. The harness looks for failures in `fails_in`;
    when that disagrees with where the failure actually surfaces, the scenario
    can never reproduce no matter how well it works."""
    if scenario.chaos_mode is None:
        return

    surfaces_in = CROSS_COMPONENT.get(
        scenario.chaos_mode, MODES[scenario.chaos_mode]["component"]
    )

    if scenario.fails_in == "latency":
        # Latency is measured at the caller, so the API is the only place the
        # harness can observe it regardless of where the sleep happens.
        assert surfaces_in == "api", \
            f"{scenario.id} measures latency but {scenario.chaos_mode} " \
            f"surfaces in {surfaces_in}"
    else:
        assert scenario.fails_in == surfaces_in, (
            f"{scenario.id} checks {scenario.fails_in} for failures but "
            f"{scenario.chaos_mode} surfaces in {surfaces_in} — it can never "
            f"reproduce"
        )


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.id)
def test_expected_cause_is_a_valid_schema_value(scenario):
    from sentry.agent.schema import VALID_CAUSES

    assert scenario.expected_cause in VALID_CAUSES
    for cause in scenario.acceptable_causes:
        assert cause in VALID_CAUSES, f"{scenario.id}: {cause!r}"


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.id)
def test_expected_remediation_is_a_valid_schema_value(scenario):
    from sentry.agent.schema import VALID_REMEDIATIONS

    assert scenario.expected_remediation in VALID_REMEDIATIONS


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.id)
def test_expectations_do_not_contradict_the_validator(scenario):
    """An expectation the schema would reject can never be satisfied — the
    agent would fail validation, repair, and still not match."""
    if scenario.expected_needs_human:
        assert scenario.expected_remediation == "none", (
            f"{scenario.id} expects escalation AND a remediation; validate() "
            f"rejects that combination, so no RCA can ever score correct"
        )
    if scenario.expected_cause == "unknown":
        assert scenario.expected_needs_human, (
            f"{scenario.id} expects 'unknown' without escalation, which "
            f"validate() rejects"
        )


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: s.id)
def test_a_scenario_that_drives_no_traffic_expects_no_failure(scenario):
    if scenario.traffic_count == 0:
        assert scenario.chaos_mode is None, (
            f"{scenario.id} arms {scenario.chaos_mode} but drives no traffic, "
            f"so nothing can trigger it"
        )


def test_the_adversarial_set_is_what_scoring_thinks_it_is():
    """scoring.summarize() hardcodes the adversarial ids. If the two drift, the
    adversarial accuracy figure silently covers the wrong scenarios."""
    import scoring

    import inspect
    source = inspect.getsource(scoring.summarize)
    declared = {s.id for s in sc.ADVERSARIAL}

    for scenario_id in declared:
        assert scenario_id in source, \
            f"{scenario_id} is adversarial but scoring does not count it as one"


def test_every_adversarial_scenario_expects_no_suspect_change():
    """That is what makes it adversarial: something looks guilty and isn't.
    A suspect_change expectation here would be testing attribution, not
    resistance to it."""
    for scenario in sc.ADVERSARIAL:
        assert scenario.expected_suspect_change is None, scenario.id


def test_the_innocent_bystander_actually_publishes_a_deploy():
    """S11's whole point is a real, recent, correlated change that did not
    cause the failure. Without publish_version_first there is no temptation
    and it degenerates into a duplicate of S14."""
    s11 = sc.by_id("S11")

    assert s11.publish_version_first is True
    assert s11.chaos_mode is None
    assert s11.expected_suspect_change is None


def test_by_id_raises_for_an_unknown_scenario():
    with pytest.raises(KeyError):
        sc.by_id("S99")
