"""Tool payload shaping — pure functions, canned inputs.

These are the functions that decide what the model sees, which makes them both
the cost lever and the accuracy lever. They are pure, so they are cheap to test
exhaustively, and every one of them was previously only exercised by a $0.15
integration run.
"""

import json
from datetime import datetime, timezone

import pytest

from sentry.agent.config import Config
from sentry.agent.tools import base, changes, logs, metrics, runbooks


# --------------------------------------------------------------------------- #
# logs._flatten — the Insights row shape
# --------------------------------------------------------------------------- #

def insights_row(**fields):
    """Insights returns each row as a list of {'field', 'value'} pairs."""
    return [{"field": k, "value": v} for k, v in fields.items()]


def test_flatten_converts_field_value_pairs_to_dicts():
    rows = logs._flatten([
        insights_row(**{
            "@timestamp": "2026-01-01 00:00:00.000",
            "level": "ERROR",
            "service": "api",
            "message": "order failed",
            "correlation_id": "c-1",
            "error_type": "KeyError",
            "stack_trace": "Traceback...",
            "order_id": "o-1",
        }),
    ])

    assert rows == [{
        "@timestamp": "2026-01-01 00:00:00.000",
        "level": "ERROR",
        "service": "api",
        "message": "order failed",
        "correlation_id": "c-1",
        "error_type": "KeyError",
        "stack_trace": "Traceback...",
        "order_id": "o-1",
    }]


def test_flatten_drops_the_internal_pointer():
    """@ptr is an Insights internal cursor. It is long, it is meaningless to the
    model, and it is resent on every subsequent turn."""
    rows = logs._flatten([insights_row(**{"@ptr": "Cm4KJQoh" * 20, "level": "ERROR"})])

    assert rows == [{"level": "ERROR"}]


def test_flatten_truncates_stack_traces():
    rows = logs._flatten([insights_row(stack_trace="x" * 5000)])
    trace = rows[0]["stack_trace"]

    assert len(trace) < 500
    assert "5000 chars total" in trace, "the model must know it saw a truncation"


def test_flatten_truncates_messages_harder_than_traces():
    rows = logs._flatten([insights_row(message="m" * 5000, stack_trace="s" * 5000)])

    assert len(rows[0]["message"]) < len(rows[0]["stack_trace"])


def test_flatten_keeps_the_raw_line_for_runtime_output():
    """A memory kill produces no application log — the process dies before the
    handler can log — so Lambda's own REPORT line is the only evidence there is.
    It is not JSON, so every named field comes back empty and the row would
    otherwise be indistinguishable from any other platform line."""
    report = ("REPORT RequestId: a96fa1ca  Duration: 1826.96 ms  "
              "Memory Size: 256 MB  Max Memory Used: 256 MB  "
              "Status: error  Error Type: Runtime.OutOfMemory")

    rows = logs._flatten([insights_row(**{"@message": report})])

    assert "Runtime.OutOfMemory" in rows[0]["@message"]


def test_flatten_drops_routine_platform_chatter():
    """START and END are emitted for every invocation and say nothing the
    metrics tool does not say better. Keeping them crowded real errors out of
    the 15-row result limit and invited the model to quote request ids back as
    findings — which is what pushed two scenarios past the output cap."""
    rows = logs._flatten([
        insights_row(**{"@message": "START RequestId: abc Version: $LATEST"}),
        insights_row(**{"@message": "END RequestId: abc"}),
        insights_row(**{"@message": "REPORT RequestId: abc Duration: 12 ms "
                                    "Billed Duration: 13 ms Memory Size: 256 MB"}),
    ])

    assert rows == []


def test_flatten_keeps_platform_lines_that_report_a_failure():
    for line in (
        "REPORT RequestId: a Status: error Error Type: Runtime.OutOfMemory",
        "2026-09-03T06:00:00Z a Task timed out after 60.00 seconds",
        "RequestId: a Error: Runtime exited with error: signal: killed",
    ):
        rows = logs._flatten([insights_row(**{"@message": line})])
        assert len(rows) == 1, line


def test_flatten_drops_the_raw_line_when_the_row_parsed():
    """Keeping both would send every structured entry twice, on every turn."""
    rows = logs._flatten([insights_row(**{
        "@message": '{"level":"ERROR","message":"boom"}',
        "level": "ERROR",
        "message": "boom",
    })])

    assert "@message" not in rows[0]
    assert rows[0]["message"] == "boom"


def test_dedupe_keeps_distinct_runtime_lines_apart():
    """Platform lines share every JSON field (they have none), so without the
    raw line in the key an OOM kill and a routine START would collapse into one
    group and the kill would vanish."""
    rows = [
        {"@message": "START RequestId: aaa Version: $LATEST"},
        {"@message": "REPORT RequestId: aaa  Error Type: Runtime.OutOfMemory"},
        {"@message": "START RequestId: bbb Version: $LATEST"},
    ]

    unique = logs._dedupe(rows)

    assert len(unique) == 3
    assert any("OutOfMemory" in u["@message"] for u in unique)


def test_flatten_handles_an_empty_result_set():
    assert logs._flatten([]) == []


def test_flatten_tolerates_missing_fields():
    """Not every log line carries a stack trace or an order id."""
    rows = logs._flatten([insights_row(level="INFO")])
    assert rows == [{"level": "INFO"}]


# --------------------------------------------------------------------------- #
# logs._dedupe — the single biggest payload win
# --------------------------------------------------------------------------- #

def test_dedupe_collapses_identical_failures():
    """16 copies of one KeyError is one fact, and the raw copies are resent on
    every subsequent turn of the loop."""
    rows = [
        {"@timestamp": f"2026-01-01 00:0{i}:00.000", "level": "ERROR",
         "error_type": "KeyError", "message": "missing customer_tier",
         "stack_trace": "Traceback (most recent call last): ..."}
        for i in range(9)
    ]

    unique = logs._dedupe(rows)

    assert len(unique) == 1
    assert unique[0]["occurrences"] == 9
    assert unique[0]["first_seen"] == "2026-01-01 00:00:00.000"
    assert unique[0]["last_seen"] == "2026-01-01 00:08:00.000"


def test_dedupe_time_span_is_independent_of_sort_order():
    """The query sorts newest-first, but nothing else should depend on that.
    A span that silently inverts would tell the model the errors stopped before
    they started."""
    ascending = [
        {"@timestamp": f"2026-01-01 00:0{i}:00.000", "level": "ERROR",
         "error_type": "KeyError", "message": "m"}
        for i in range(5)
    ]
    descending = list(reversed(ascending))

    for rows in (ascending, descending):
        group = logs._dedupe(rows)[0]
        assert group["first_seen"] == "2026-01-01 00:00:00.000"
        assert group["last_seen"] == "2026-01-01 00:04:00.000"


def test_dedupe_drops_the_redundant_timestamp_field():
    """first_seen/last_seen supersede it, and it is resent on every turn."""
    group = logs._dedupe([{"@timestamp": "2026-01-01 00:00:00.000", "level": "ERROR"}])[0]

    assert "@timestamp" not in group


def test_dedupe_samples_correlation_ids_across_the_group():
    """Cross-service tracing has to survive deduplication — it is how the model
    ties a consumer failure back to the API request that caused it."""
    rows = [
        {"level": "ERROR", "message": "m", "correlation_id": f"c-{i}"}
        for i in range(9)
    ]

    group = logs._dedupe(rows)[0]

    assert group["occurrences"] == 9
    assert len(group["correlation_ids"]) == logs.MAX_CORRELATION_SAMPLES
    assert group["correlation_ids"][0] == "c-0"


def test_dedupe_omits_the_sample_list_when_there_is_nothing_to_sample():
    """One distinct id means the plural list only repeats the singular field."""
    rows = [{"level": "ERROR", "message": "m", "correlation_id": "c-1"}] * 3

    group = logs._dedupe(rows)[0]

    assert group["correlation_id"] == "c-1"
    assert "correlation_ids" not in group


def test_dedupe_keeps_genuinely_different_failures():
    rows = [
        {"level": "ERROR", "error_type": "KeyError", "message": "a"},
        {"level": "ERROR", "error_type": "TypeError", "message": "b"},
        {"level": "WARNING", "error_type": "KeyError", "message": "a"},
    ]

    assert len(logs._dedupe(rows)) == 3


def test_dedupe_groups_on_the_trace_prefix_only():
    """Two traces identical for the first 200 chars are the same defect even if
    they diverge later — line numbers in deep frames vary between requests."""
    shared = "Traceback:\n" + "frame\n" * 60
    rows = [
        {"level": "ERROR", "error_type": "KeyError", "message": "m",
         "stack_trace": shared + "unique-tail-1"},
        {"level": "ERROR", "error_type": "KeyError", "message": "m",
         "stack_trace": shared + "unique-tail-2"},
    ]

    assert len(logs._dedupe(rows)) == 1


def test_dedupe_handles_missing_stack_traces():
    rows = [{"level": "INFO", "message": "same"}, {"level": "INFO", "message": "same"}]

    unique = logs._dedupe(rows)

    assert len(unique) == 1
    assert unique[0]["occurrences"] == 2


def test_dedupe_of_nothing_is_nothing():
    assert logs._dedupe([]) == []


def test_dedupe_preserves_the_original_fields():
    rows = [{"level": "ERROR", "correlation_id": "c-1", "order_id": "o-1",
             "message": "m"}]

    assert logs._dedupe(rows)[0]["correlation_id"] == "c-1"


# --------------------------------------------------------------------------- #
# logs._build_query — never built from model input
# --------------------------------------------------------------------------- #

def test_query_filters_by_level():
    assert "filter level = 'ERROR'" in logs._build_query("ERROR", None)


def test_query_omits_the_level_filter_for_all():
    assert "filter level =" not in logs._build_query("all", None)


def test_unfiltered_query_excludes_routine_platform_lines():
    """The row limit is applied by Insights, so noise has to be excluded in the
    QUERY. Filtering it afterwards spends the budget fetching START/END lines
    that are then discarded, leaving the agent two or three rows and a report
    of insufficient evidence — which dropped abstention accuracy to 0.167."""
    query = logs._build_query("all", None)

    assert "ispresent(level)" in query
    assert "Runtime.OutOfMemory" in query
    # ...but the failure lines must survive, or S07 loses its only evidence.
    assert "or @message like" in query


def test_level_filtered_query_needs_no_noise_filter():
    """level = 'ERROR' already excludes platform lines, which carry no level."""
    query = logs._build_query("ERROR", None)

    assert "filter level = 'ERROR'" in query
    assert "ispresent" not in query


def test_query_always_bounds_the_result_count():
    assert f"limit {Config.LOG_QUERY_LIMIT}" in logs._build_query(None, None)


def test_query_escapes_the_search_term():
    """The term reaches an Insights regex literal. A slash in it would terminate
    the pattern early and change the query the model asked for."""
    query = logs._build_query(None, "a/b")

    assert r"a\/b" in query


def test_query_escapes_backslashes_before_slashes():
    assert logs._build_query(None, "a\\b").count("\\\\") == 1


# --------------------------------------------------------------------------- #
# metrics._error_rate — the load-vs-defect signal
# --------------------------------------------------------------------------- #

def series(*pairs):
    return [{"timestamp": ts, "value": value} for ts, value in pairs]


def test_error_rate_divides_errors_by_invocations():
    rate = metrics._error_rate({
        "Invocations": series((100, 40.0), (160, 50.0)),
        "Errors": series((100, 4.0), (160, 25.0)),
    })

    assert rate == [
        {"timestamp": 100, "value": 0.1},
        {"timestamp": 160, "value": 0.5},
    ]


def test_error_rate_treats_a_missing_error_bucket_as_zero():
    """No Errors datapoint means no errors, not missing data."""
    rate = metrics._error_rate({
        "Invocations": series((100, 40.0), (160, 40.0)),
        "Errors": series((160, 8.0)),
    })

    assert rate == [
        {"timestamp": 100, "value": 0.0},
        {"timestamp": 160, "value": 0.2},
    ]


def test_error_rate_skips_buckets_with_no_invocations():
    """0/0 is not 'no errors' — it is no traffic, and emitting 0.0 would tell the
    model the service was healthy during an outage."""
    rate = metrics._error_rate({
        "Invocations": series((100, 0.0), (160, 10.0)),
        "Errors": series((100, 0.0), (160, 1.0)),
    })

    assert [p["timestamp"] for p in rate] == [160]


def test_error_rate_is_ordered_by_time():
    rate = metrics._error_rate({
        "Invocations": series((300, 10.0), (100, 10.0), (200, 10.0)),
        "Errors": series((300, 1.0), (100, 2.0), (200, 3.0)),
    })

    assert [p["timestamp"] for p in rate] == [100, 200, 300]


def test_error_rate_is_empty_without_invocations():
    assert metrics._error_rate({"Errors": series((100, 5.0))}) == []


def test_error_rate_distinguishes_load_from_defect():
    """The decoy scenario, stated as an assertion.

    Load spike: errors and invocations both rise, the ratio stays flat.
    Defect:     invocations flat, errors rise, the ratio climbs.
    """
    load = metrics._error_rate({
        "Invocations": series((0, 100.0), (60, 400.0), (120, 800.0)),
        "Errors": series((0, 2.0), (60, 8.0), (120, 16.0)),
    })
    defect = metrics._error_rate({
        "Invocations": series((0, 100.0), (60, 100.0), (120, 100.0)),
        "Errors": series((0, 1.0), (60, 40.0), (120, 90.0)),
    })

    assert [p["value"] for p in load] == [0.02, 0.02, 0.02]
    assert [p["value"] for p in defect] == [0.01, 0.4, 0.9]


# --------------------------------------------------------------------------- #
# metrics._summarize
# --------------------------------------------------------------------------- #

def test_summarize_reports_totals_and_shape():
    summary = metrics._summarize(series((0, 1.0), (60, 2.0), (120, 3.0), (180, 4.0)))

    assert summary["total"] == 10.0
    assert summary["max"] == 4.0
    assert summary["mean"] == 2.5
    assert summary["datapoints"] == 4


def test_summarize_detects_a_rising_trend():
    assert metrics._summarize(
        series((0, 1.0), (60, 1.0), (120, 40.0), (180, 50.0))
    )["trend"] == "rising"


def test_summarize_detects_a_falling_trend():
    assert metrics._summarize(
        series((0, 50.0), (60, 40.0), (120, 1.0), (180, 1.0))
    )["trend"] == "falling"


def test_summarize_detects_a_flat_trend():
    assert metrics._summarize(
        series((0, 10.0), (60, 10.0), (120, 10.0), (180, 10.0))
    )["trend"] == "flat"


def test_summarize_of_nothing_is_none():
    assert metrics._summarize([]) is None


def test_summarize_handles_a_single_datapoint():
    """len//2 is 0 for one point; the `or 1` guard stops a zero-length slice."""
    summary = metrics._summarize(series((0, 5.0)))

    assert summary["datapoints"] == 1
    assert summary["total"] == 5.0


# --------------------------------------------------------------------------- #
# metrics._compact_series — dense encoding, same information
# --------------------------------------------------------------------------- #

def test_compact_series_preserves_every_value():
    compact = metrics._compact_series(series((100, 1.0), (160, 2.0), (220, 3.0)), 60)

    assert compact["values"] == [1.0, 2.0, 3.0]
    assert compact["start_timestamp"] == 100
    assert compact["period_seconds"] == 60


def test_compact_series_timestamps_remain_reconstructable():
    """Nothing is lost: bucket n is start + n * period."""
    points = series((1000, 5.0), (1060, 6.0), (1120, 7.0))
    compact = metrics._compact_series(points, 60)

    rebuilt = [
        compact["start_timestamp"] + i * compact["period_seconds"]
        for i in range(len(compact["values"]))
    ]
    assert rebuilt == [p["timestamp"] for p in points]


def test_compact_series_of_nothing_is_none():
    assert metrics._compact_series([], 60) is None


def test_compact_encoding_is_smaller_than_the_verbose_one():
    """The whole reason it exists. Repeated key names dominate the verbose form
    and are resent on every turn of the loop."""
    points = series(*[(i * 60, float(i)) for i in range(11)])

    verbose = len(json.dumps(points))
    compact = len(json.dumps(metrics._compact_series(points, 60)))

    assert compact < verbose / 2


# --------------------------------------------------------------------------- #
# metrics._correlate — the load-vs-defect verdict, decided in code
# --------------------------------------------------------------------------- #

def _correlate_for(invocations, errors):
    raw = {"Invocations": invocations, "Errors": errors}
    return metrics._correlate(raw, metrics._error_rate(raw))


def test_correlate_calls_a_load_spike_proportional():
    """The decoy scenario. Traffic 8x, errors 8x, rate unchanged — not a defect."""
    verdict = _correlate_for(
        series((0, 100.0), (60, 100.0), (120, 800.0), (180, 800.0)),
        series((0, 2.0), (60, 2.0), (120, 16.0), (180, 16.0)),
    )

    assert verdict["verdict"] == "errors_tracked_load"
    assert verdict["error_rate_first_half"] == verdict["error_rate_second_half"]
    assert verdict["invocations_change_pct"] == 700.0


def test_correlate_calls_a_defect_disproportionate():
    """Traffic flat, errors climbing — volume does not explain it."""
    verdict = _correlate_for(
        series((0, 100.0), (60, 100.0), (120, 100.0), (180, 100.0)),
        series((0, 1.0), (60, 1.0), (120, 45.0), (180, 60.0)),
    )

    assert verdict["verdict"] == "errors_outpaced_load"
    assert verdict["invocations_change_pct"] == 0.0
    assert verdict["error_rate_second_half"] > verdict["error_rate_first_half"]


def test_correlate_detects_a_defect_even_while_traffic_rises():
    """The hard case: traffic doubled AND the code broke. Proportionality, not
    the raw error count, is what separates them."""
    verdict = _correlate_for(
        series((0, 100.0), (60, 100.0), (120, 200.0), (180, 200.0)),
        series((0, 1.0), (60, 1.0), (120, 100.0), (180, 100.0)),
    )

    assert verdict["verdict"] == "errors_outpaced_load"


def test_correlate_flags_a_saturated_error_rate_as_not_load():
    """The 2026-09-01 e2e run: every request failed before and after a traffic
    rise, so the rate never "rose" and the old comparison reported a dead
    service as load-correlated. The model had to override the tool to get the
    right answer."""
    verdict = _correlate_for(
        series((0, 4.0), (60, 4.0), (120, 10.0), (180, 10.0)),
        series((0, 4.0), (60, 4.0), (120, 10.0), (180, 10.0)),
    )

    assert verdict["error_rate_first_half"] == 1.0
    assert verdict["error_rate_second_half"] == 1.0
    assert verdict["verdict"] == "errors_saturated"
    assert "does not explain" in verdict["interpretation"]


def test_saturated_verdict_does_not_shadow_a_genuine_rise():
    """A rate that climbs into saturation is still better described as having
    outpaced load — the rise is the more specific fact."""
    verdict = _correlate_for(
        series((0, 100.0), (60, 100.0), (120, 100.0), (180, 100.0)),
        series((0, 1.0), (60, 1.0), (120, 90.0), (180, 95.0)),
    )

    assert verdict["verdict"] == "errors_outpaced_load"


def test_a_low_but_flat_error_rate_is_still_load():
    """The saturation floor must not swallow the load-spike decoy."""
    verdict = _correlate_for(
        series((0, 100.0), (60, 100.0), (120, 800.0), (180, 800.0)),
        series((0, 2.0), (60, 2.0), (120, 16.0), (180, 16.0)),
    )

    assert verdict["verdict"] == "errors_tracked_load"


def test_correlate_reports_no_errors_plainly():
    """A genuinely quiet resource: no errors and flat duration."""
    verdict = _correlate_for(
        series((0, 100.0), (60, 100.0)),
        series((0, 0.0), (60, 0.0)),
    )

    assert verdict["verdict"] == "no_errors"
    assert "did not change materially" in verdict["interpretation"]


# --------------------------------------------------------------------------- #
# the latency axis — zero errors does not mean nothing failed
# --------------------------------------------------------------------------- #

def test_duration_shift_reports_the_move():
    shift = metrics._duration_shift(
        {"Duration": series((0, 20.0), (60, 20.0), (120, 400.0), (180, 600.0))}
    )

    assert shift["duration_before_ms"] == 20.0
    assert shift["duration_after_ms"] == 500.0
    assert shift["duration_change_pct"] == 2400.0


def test_duration_shift_without_duration_data_is_none():
    assert metrics._duration_shift({"Invocations": series((0, 5.0))}) is None


def test_no_errors_with_rising_duration_points_at_latency():
    """The S04 shape: a latency alarm with a flat error rate. Reporting only
    'no errors' here reads as 'this resource is healthy' and sends the model to
    the wrong component — which is what made the latency scenario expensive."""
    raw = {
        "Invocations": series((0, 40.0), (60, 40.0), (120, 40.0), (180, 40.0)),
        "Errors": series((0, 0.0), (60, 0.0), (120, 0.0), (180, 0.0)),
        "Duration": series((0, 24.0), (60, 25.0), (120, 4500.0), (180, 4600.0)),
    }

    verdict = metrics._correlate(raw, metrics._error_rate(raw))

    assert verdict["verdict"] == "no_errors"
    assert verdict["duration_after_ms"] > verdict["duration_before_ms"]
    assert "latency" in verdict["interpretation"].lower()
    # The specific regression: never imply the resource is uninvolved.
    assert "did not fail here" not in verdict["interpretation"]


def test_duration_is_reported_alongside_an_error_verdict():
    """Both axes travel together — a defect can raise errors and latency."""
    raw = {
        "Invocations": series((0, 100.0), (60, 100.0), (120, 100.0), (180, 100.0)),
        "Errors": series((0, 1.0), (60, 1.0), (120, 45.0), (180, 60.0)),
        "Duration": series((0, 20.0), (60, 20.0), (120, 90.0), (180, 95.0)),
    }

    verdict = metrics._correlate(raw, metrics._error_rate(raw))

    assert verdict["verdict"] == "errors_outpaced_load"
    assert "duration_change_pct" in verdict


def test_metrics_payload_surfaces_latency_for_a_zero_error_window(incident, monkeypatch):
    monkeypatch.setattr(metrics, "_fetch_series", _fake_metric_series(
        Invocations=series((0, 40.0), (60, 40.0), (120, 40.0), (180, 40.0)),
        Duration=series((0, 24.0), (60, 25.0), (120, 4500.0), (180, 4600.0)),
    ))

    result = metrics.run(incident, target="api")

    assert result["load_vs_defect"]["verdict"] == "no_errors"
    assert result["load_vs_defect"]["duration_after_ms"] > 1000
    assert "Duration" in result["series"]


def test_correlate_without_invocations_is_insufficient_not_a_guess():
    verdict = _correlate_for([], series((0, 5.0)))

    assert verdict["verdict"] == "insufficient_data"


def test_correlate_ignores_rounding_noise_at_tiny_error_counts():
    """1 error in 5000 rising to 2 in 5000 is a 100% relative jump and means
    nothing. Without the absolute floor this would read as a defect."""
    verdict = _correlate_for(
        series((0, 5000.0), (60, 5000.0), (120, 5000.0), (180, 5000.0)),
        series((0, 1.0), (60, 1.0), (120, 2.0), (180, 2.0)),
    )

    assert verdict["verdict"] == "errors_tracked_load"


def test_correlate_survives_zero_traffic_in_the_first_half():
    """Percentage change is undefined, not zero — and must not raise."""
    verdict = _correlate_for(
        series((0, 0.0), (60, 0.0), (120, 100.0), (180, 100.0)),
        series((0, 0.0), (60, 0.0), (120, 1.0), (180, 1.0)),
    )

    assert verdict["invocations_change_pct"] is None
    assert "undefined" in verdict["interpretation"]


def test_saturated_rate_with_undefined_traffic_change_does_not_raise():
    """The saturated branch omits the traffic figure by design — traffic is what
    it is asserting to be irrelevant — but it must still survive a None."""
    verdict = _correlate_for(
        series((0, 0.0), (60, 0.0), (120, 100.0), (180, 100.0)),
        series((0, 0.0), (60, 0.0), (120, 90.0), (180, 90.0)),
    )

    assert verdict["verdict"] == "errors_saturated"
    assert verdict["invocations_change_pct"] is None


def test_correlate_states_evidence_without_naming_a_root_cause():
    """Abstention is a first-class output. A tool that announces the conclusion
    pressures the model into concluding, which is exactly the failure mode the
    abstention axis measures."""
    verdict = _correlate_for(
        series((0, 100.0), (60, 100.0), (120, 100.0), (180, 100.0)),
        series((0, 1.0), (60, 1.0), (120, 45.0), (180, 60.0)),
    )

    lowered = verdict["interpretation"].lower()
    for word in ("code defect", "root cause", "caused by", "roll back", "rollback"):
        assert word not in lowered


# --------------------------------------------------------------------------- #
# changes._relevant — the shared-account guard
# --------------------------------------------------------------------------- #

def cloudtrail_event(resource_names=(), raw=None, name="UpdateFunctionCode20150331v2"):
    event = {
        "EventId": "e-1",
        "EventName": name,
        "Username": "gulsher",
        "Resources": [{"ResourceName": n} for n in resource_names],
    }
    if raw is not None:
        event["CloudTrailEvent"] = json.dumps(raw)
    return event


def test_relevant_accepts_a_project_resource():
    assert changes._relevant(
        cloudtrail_event(["sentry-capstone-api-gulsher"])
    )


def test_relevant_rejects_another_teams_deploy():
    """CloudTrail is account-wide with no resource-level IAM. Without this the
    model treats a colleague's unrelated deploy as a suspect change."""
    assert not changes._relevant(
        cloudtrail_event(["some-other-team-etl-loader"])
    )


def test_relevant_finds_the_project_inside_the_raw_payload():
    """Some events carry the target only in requestParameters."""
    event = cloudtrail_event(
        [], raw={"requestParameters": {"functionName": "sentry-capstone-consumer-gulsher"}}
    )

    assert changes._relevant(event)


def test_relevant_rejects_a_raw_payload_for_another_workload():
    event = cloudtrail_event([], raw={"requestParameters": {"functionName": "unrelated-fn"}})

    assert not changes._relevant(event)


@pytest.mark.parametrize("name", [
    # The literal name from the run that leaked. The filter matched full
    # resource names as substrings, and "sentry-capstone-executor-gulsher" is
    # NOT a substring of this — the role carries "-role-" in the MIDDLE. The
    # agent duly offered this role's policy change as a candidate cause for a
    # target-app failure, on a scenario whose whole point is that nothing is
    # wrong.
    "sentry-capstone-executor-role-gulsher",
    "sentry-capstone-approval-role-gulsher",
    # The shape Terraform produces, which differs again.
    "sentry-capstone-executor-gulsher-role",
    "sentry-capstone-approval-gulsher-role",
    # And the shapes these resources take elsewhere.
    "/aws/lambda/sentry-capstone-ingest-gulsher",
    "arn:aws:sqs:us-east-1:000000000000:sentry-capstone-work-dlq-gulsher",
])
def test_own_infrastructure_is_excluded_whatever_the_name_shape(name):
    """Match component tokens, not assembled names. Substring matching failed
    silently — a name that did not fit the expected arrangement simply passed
    through, and nothing in the output said the filter had not applied."""
    assert not changes._relevant(cloudtrail_event([name])), name


@pytest.mark.parametrize("name", [
    "sentry-capstone-api-gulsher",
    "sentry-capstone-consumer-gulsher",
    "sentry-capstone-app-gulsher",
    "sentry-capstone-orders-dlq-gulsher",
])
def test_target_app_resources_survive_the_token_filter(name):
    """The filter must not over-reach: these are what the agent investigates."""
    assert changes._relevant(cloudtrail_event([name])), name


def test_relevant_rejects_sentrys_own_deploys():
    """Sentry's components share the name prefix but are downstream of the
    target app, so they cannot cause its failures. During a sweep they are also
    the biggest source of change noise — the 2026-09-01 run surfaced 23 events,
    mostly the pipeline redeploying itself."""
    for own in ("sentry-capstone-agent-gulsher", "sentry-capstone-ingest-gulsher",
                "sentry-capstone-work-gulsher", "sentry-capstone-incidents-gulsher"):
        assert not changes._relevant(cloudtrail_event([own])), own


def test_relevant_still_accepts_target_app_deploys():
    for target in ("sentry-capstone-api-gulsher", "sentry-capstone-consumer-gulsher",
                   "sentry-capstone-orders-gulsher"):
        assert changes._relevant(cloudtrail_event([target])), target


def test_relevant_rejects_own_infrastructure_named_only_in_the_payload():
    event = cloudtrail_event(
        [], raw={"requestParameters": {"functionName": "sentry-capstone-agent-gulsher"}}
    )

    assert not changes._relevant(event)


def test_a_mixed_event_touching_the_target_app_is_kept():
    """An event naming both is still relevant — the target app was touched."""
    assert changes._relevant(
        cloudtrail_event(["sentry-capstone-agent-gulsher",
                          "sentry-capstone-api-gulsher"])
    )


def test_deployment_entries_carry_one_timestamp(incident, monkeypatch):
    """Two renderings of the same instant is one of them resent every turn."""
    occurred = datetime(2026, 9, 1, 7, 39, 20, tzinfo=timezone.utc)
    monkeypatch.setattr(changes._ct, "lookup_events", lambda **kw: {
        "Events": [{
            "EventId": "e-1", "EventName": "UpdateFunctionCode20150331v2",
            "EventTime": occurred, "Username": "gulsher",
            "Resources": [{"ResourceName": "sentry-capstone-api-gulsher"}],
        }]
    })

    entry = changes._fetch_deployments(occurred, occurred)[0]

    assert entry["occurred_at_iso"].startswith("2026-09-01T07:39:20")
    assert "occurred_at" not in entry


def test_relevant_rejects_an_event_with_no_resources_at_all():
    assert not changes._relevant({"EventId": "e-1", "EventName": "PutRolePolicy"})


def test_relevant_handles_null_resources():
    assert not changes._relevant({"Resources": None, "CloudTrailEvent": None})


def test_relevant_accepts_a_mixed_resource_list():
    assert changes._relevant(
        cloudtrail_event(["unrelated-thing", "sentry-capstone-consumer-gulsher"])
    )


def test_relevant_filters_a_realistic_mixed_batch():
    events = [
        cloudtrail_event(["sentry-capstone-api-gulsher"]),
        cloudtrail_event(["colleague-glue-job"]),
        # A target-app role named only in the payload. This used to be the
        # AGENT's role, which was correct when any sentry-capstone-* resource
        # counted as evidence — the own-infrastructure filter now drops that,
        # so the fixture has to name something the agent actually investigates.
        cloudtrail_event([], raw={"requestParameters": {"roleName": "sentry-capstone-consumer-role-gulsher"}}),
        cloudtrail_event([], raw={"requestParameters": {"roleName": "AmplifyDeployRole"}}),
        cloudtrail_event(["sentry-capstone-consumer-gulsher"]),
    ]

    assert [changes._relevant(e) for e in events] == [True, False, True, False, True]


def test_extract_target_prefers_the_named_project_resource():
    target = changes._extract_target(
        cloudtrail_event(["unrelated", "sentry-capstone-api-gulsher"])
    )

    assert target == "sentry-capstone-api-gulsher"


def test_extract_target_falls_back_to_request_parameters():
    event = cloudtrail_event(
        [], raw={"requestParameters": {"functionName": "sentry-capstone-api-gulsher"}}
    )

    assert changes._extract_target(event) == "sentry-capstone-api-gulsher"


def test_extract_target_returns_none_on_malformed_payload():
    assert changes._extract_target({"CloudTrailEvent": "not json"}) is None


def test_deploy_events_cover_the_remediation_actions():
    """The executor shifts aliases and publishes versions. If those event names
    were missing, the agent could not see its own remediation in the history."""
    assert "UpdateAlias20150331" in changes.DEPLOY_EVENTS
    assert "PublishVersion20150331" in changes.DEPLOY_EVENTS
    assert "UpdateFunctionCode20150331v2" in changes.DEPLOY_EVENTS


def test_commit_query_dates_are_url_safe(monkeypatch):
    """datetime.isoformat() renders UTC as '+00:00', and a bare '+' decodes to a
    space in a query string — GitHub then matched nothing and the commits path
    returned empty with no error, which is how it failed silently in production."""
    captured = {}

    monkeypatch.setenv("GITHUB_REPO", "owner/repo")
    monkeypatch.setattr(changes, "_github_token", lambda: "tok")
    monkeypatch.setattr(changes, "_github_get",
                        lambda url, token: captured.setdefault("url", url) and None)

    changes._fetch_commits(
        datetime(2026, 8, 31, 7, 50, tzinfo=timezone.utc),
        datetime(2026, 9, 1, 7, 50, tzinfo=timezone.utc),
    )

    url = captured["url"]
    assert "+" not in url, f"unencoded '+' decodes to a space: {url}"
    assert "since=2026-08-31T07%3A50%3A00Z" in url
    assert "until=2026-09-01T07%3A50%3A00Z" in url


def test_commit_detail_budget_stops_before_the_lambda_timeout(monkeypatch):
    """One request per commit meant the worst case scaled with repo activity —
    at 10 commits it exceeded the 120s Lambda limit and killed S06 outright.
    The budget must bound it regardless of how many commits come back."""
    monkeypatch.setenv("GITHUB_REPO", "owner/repo")
    monkeypatch.setattr(changes, "_github_token", lambda: "tok")
    monkeypatch.setattr(changes, "COMMIT_DETAIL_BUDGET_S", 0)

    listing = [{"sha": f"{i:040x}", "commit": {"message": f"c{i}", "author": {}}}
               for i in range(5)]
    detail_calls = []

    def fake_get(url, token):
        if "/commits/" in url:
            detail_calls.append(url)
            return {"files": [{"filename": "a.py"}]}
        return listing

    monkeypatch.setattr(changes, "_github_get", fake_get)

    commits = changes._fetch_commits(
        datetime(2026, 9, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 2, tzinfo=timezone.utc),
    )

    assert len(commits) == 5, "commits themselves are still returned"
    assert detail_calls == [], "no detail request should run once the budget is gone"
    # An empty changed_files would read as "this commit touched nothing".
    assert all(c["changed_files_unavailable"] for c in commits)


def test_commit_details_are_fetched_when_the_budget_allows(monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", "owner/repo")
    monkeypatch.setattr(changes, "_github_token", lambda: "tok")

    listing = [{"sha": "a" * 40, "commit": {"message": "c", "author": {}}}]

    def fake_get(url, token):
        return {"files": [{"filename": "src/target_app/api/handler.py"}]} \
            if "/commits/" in url else listing

    monkeypatch.setattr(changes, "_github_get", fake_get)

    commit = changes._fetch_commits(
        datetime(2026, 9, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 2, tzinfo=timezone.utc),
    )[0]

    assert commit["changed_files"] == ["src/target_app/api/handler.py"]
    assert "changed_files_unavailable" not in commit


def test_changes_returns_no_commits_when_github_is_unconfigured(incident, monkeypatch):
    """GITHUB_REPO is unset today; the tool must degrade rather than fail."""
    monkeypatch.setattr(changes, "_fetch_deployments", lambda start, end: [])

    result = changes.run_changes(incident, source="commits")

    assert result["result_count"] == 0
    assert "github" not in result["sources_searched"]
    assert "note" in result, "the correlation warning must survive the empty case"


def test_changes_caps_the_lookback_window(incident, monkeypatch):
    """An unbounded lookback is an unbounded CloudTrail scan."""
    captured = {}

    def fake_fetch(start, end):
        captured["hours"] = round((end - start).total_seconds() / 3600)
        return []

    monkeypatch.setattr(changes, "_fetch_deployments", fake_fetch)

    result = changes.run_changes(incident, source="deployments", lookback_hours=999)

    assert captured["hours"] == changes.MAX_LOOKBACK_HOURS
    assert result["window"]["lookback_hours"] == changes.MAX_LOOKBACK_HOURS


def test_changes_are_sorted_newest_first(incident, monkeypatch):
    monkeypatch.setattr(changes, "_fetch_deployments", lambda s, e: [
        {"kind": "deployment", "occurred_at_iso": "2026-01-01T10:00:00+00:00"},
        {"kind": "deployment", "occurred_at_iso": "2026-01-01T12:00:00+00:00"},
        {"kind": "deployment", "occurred_at_iso": "2026-01-01T11:00:00+00:00"},
    ])

    result = changes.run_changes(incident, source="deployments")

    assert [c["occurred_at_iso"] for c in result["changes"]] == [
        "2026-01-01T12:00:00+00:00",
        "2026-01-01T11:00:00+00:00",
        "2026-01-01T10:00:00+00:00",
    ]


# --------------------------------------------------------------------------- #
# the assembled payloads — what the model actually receives
# --------------------------------------------------------------------------- #

def test_logs_payload_returns_deduplicated_entries(incident, monkeypatch):
    """The dedup was computed and then discarded, so 15 identical stack traces
    were sent on every turn. This is the assertion that keeps it wired up."""
    rows = [
        {"@timestamp": f"2026-01-01 00:0{i}:00.000", "level": "ERROR",
         "error_type": "KeyError", "message": "missing customer_tier",
         "stack_trace": "Traceback: ..."}
        for i in range(9)
    ]
    monkeypatch.setattr(logs, "_run_query", lambda *a, **k: (rows, "Complete"))

    result = logs.run(incident, log_group="api")

    assert len(result["entries"]) == 1, "identical failures must be collapsed"
    assert result["entries"][0]["occurrences"] == 9
    # The raw count is still reported, so nothing about scale is hidden.
    assert result["result_count"] == 9
    assert result["unique_patterns"] == 1
    assert result["level_counts"] == {"ERROR": 9}


def test_logs_payload_shrinks_substantially_on_repetitive_errors(incident, monkeypatch):
    """The size claim, asserted rather than assumed."""
    rows = [
        {"@timestamp": f"2026-01-01 00:{i:02d}:00.000", "level": "ERROR",
         "service": "api", "correlation_id": f"c-{i}", "order_id": f"o-{i}",
         "error_type": "KeyError", "message": "missing customer_tier",
         "stack_trace": "Traceback (most recent call last):\n" + "  frame\n" * 20}
        for i in range(15)
    ]
    monkeypatch.setattr(logs, "_run_query", lambda *a, **k: (rows, "Complete"))

    result = logs.run(incident, log_group="api")

    before = len(json.dumps(rows))
    after = len(json.dumps(result["entries"]))
    assert after < before / 4, f"expected a large reduction, got {before} -> {after}"


def test_logs_payload_keeps_distinct_failures_separate(incident, monkeypatch):
    """Deduplication must never merge two different defects into one."""
    rows = [
        {"@timestamp": "2026-01-01 00:00:00.000", "level": "ERROR",
         "error_type": "KeyError", "message": "a"},
        {"@timestamp": "2026-01-01 00:01:00.000", "level": "ERROR",
         "error_type": "AccessDeniedException", "message": "b"},
    ]
    monkeypatch.setattr(logs, "_run_query", lambda *a, **k: (rows, "Complete"))

    result = logs.run(incident, log_group="api")

    assert result["unique_patterns"] == 2
    assert {e["error_type"] for e in result["entries"]} == {
        "KeyError", "AccessDeniedException"
    }


def test_logs_payload_reports_truncation(incident, monkeypatch):
    """'I saw 15 of possibly many' is different evidence from 'I saw all 3'."""
    rows = [{"level": "ERROR", "message": f"m{i}"} for i in range(Config.LOG_QUERY_LIMIT)]
    monkeypatch.setattr(logs, "_run_query", lambda *a, **k: (rows, "Complete"))

    assert logs.run(incident, log_group="api")["truncated"] is True


def test_logs_payload_survives_an_empty_result(incident, monkeypatch):
    monkeypatch.setattr(logs, "_run_query", lambda *a, **k: ([], "Complete"))
    monkeypatch.setattr(logs.time, "sleep", lambda s: None)

    result = logs.run(incident, log_group="api")

    assert result["entries"] == []
    assert result["result_count"] == 0
    assert result["level_counts"] == {}


def test_empty_level_search_reports_what_the_window_does_contain(incident, monkeypatch):
    """'No ERROR entries' and 'the search failed' look identical to the model
    unless the level distribution is stated. Leaving it ambiguous costs a turn,
    which costs far more than the extra Insights query."""
    calls = []

    def fake_run_query(groups, query, start, end):
        calls.append(query)
        if "stats count" in query:
            return ([{"level": "INFO", "entries": "240"},
                     {"level": "WARNING", "entries": "3"}], "Complete")
        return ([], "Complete")

    monkeypatch.setattr(logs, "_run_query", fake_run_query)
    monkeypatch.setattr(logs.time, "sleep", lambda s: None)

    result = logs.run(incident, log_group="api", level="ERROR")

    assert result["window_level_counts"] == {"INFO": 240, "WARNING": 3}
    assert "ERROR" not in result["window_level_counts"]
    assert "not that the search failed" in result["note"]
    assert any("stats count" in q for q in calls)


def test_level_census_is_skipped_when_results_were_found(incident, monkeypatch):
    """The census is a fallback, not a second query on every search."""
    calls = []

    def fake_run_query(groups, query, start, end):
        calls.append(query)
        return ([{"level": "ERROR", "message": "boom"}], "Complete")

    monkeypatch.setattr(logs, "_run_query", fake_run_query)

    result = logs.run(incident, log_group="api", level="ERROR")

    assert "window_level_counts" not in result
    assert not any("stats count" in q for q in calls)


def test_level_census_is_skipped_for_unfiltered_searches(incident, monkeypatch):
    """With level='all' an empty result already means the window is empty."""
    calls = []

    def fake_run_query(groups, query, start, end):
        calls.append(query)
        return ([], "Complete")

    monkeypatch.setattr(logs, "_run_query", fake_run_query)
    monkeypatch.setattr(logs.time, "sleep", lambda s: None)

    result = logs.run(incident, log_group="api", level="all")

    assert "window_level_counts" not in result
    assert not any("stats count" in q for q in calls)


def test_level_census_failure_does_not_break_the_search(incident, monkeypatch):
    """A tool must never raise for an expected condition."""
    def fake_run_query(groups, query, start, end):
        if "stats count" in query:
            raise RuntimeError("Insights unavailable")
        return ([], "Complete")

    monkeypatch.setattr(logs, "_run_query", fake_run_query)
    monkeypatch.setattr(logs.time, "sleep", lambda s: None)

    result = logs.run(incident, log_group="api", level="ERROR")

    assert result["result_count"] == 0
    assert "window_level_counts" not in result


def _fake_metric_series(**by_metric):
    def fetch(namespace, metric, dimensions, start, end, period, stat):
        return by_metric.get(metric, [])
    return fetch


def test_metrics_payload_keeps_the_raw_shape_where_it_matters(incident, monkeypatch):
    """Duration and ErrorRate are the two series the model reasons over. An
    earlier version summarised them away and the agent compensated with more
    tool calls, which cost more than the series did."""
    monkeypatch.setattr(metrics, "_fetch_series", _fake_metric_series(
        Invocations=series((0, 100.0), (60, 100.0), (120, 100.0), (180, 100.0)),
        Errors=series((0, 1.0), (60, 1.0), (120, 45.0), (180, 60.0)),
        Duration=series((0, 20.0), (60, 21.0), (120, 400.0), (180, 900.0)),
    ))

    result = metrics.run(incident, target="api")

    assert "ErrorRate" in result["series"]
    assert "Duration" in result["series"]
    assert result["series"]["Duration"]["values"] == [20.0, 21.0, 400.0, 900.0]


def test_metrics_payload_summarises_the_count_metrics(incident, monkeypatch):
    """Invocations and Errors keep totals and trend only; their per-bucket shape
    is recoverable from the ErrorRate series, which shares the buckets."""
    monkeypatch.setattr(metrics, "_fetch_series", _fake_metric_series(
        Invocations=series((0, 100.0), (60, 100.0)),
        Errors=series((0, 1.0), (60, 9.0)),
    ))

    result = metrics.run(incident, target="api")

    assert result["summary"]["Invocations"]["total"] == 200.0
    assert "Invocations" not in result.get("series", {})


def test_metrics_payload_answers_load_vs_defect_without_another_call(incident, monkeypatch):
    """The constraint: this distinction is the whole point of the tool."""
    monkeypatch.setattr(metrics, "_fetch_series", _fake_metric_series(
        Invocations=series((0, 100.0), (60, 100.0), (120, 800.0), (180, 800.0)),
        Errors=series((0, 2.0), (60, 2.0), (120, 16.0), (180, 16.0)),
    ))

    result = metrics.run(incident, target="api")

    assert result["load_vs_defect"]["verdict"] == "errors_tracked_load"


def test_metrics_payload_names_the_metrics_with_no_data(incident, monkeypatch):
    """An absent metric must read as 'nothing was recorded', not as an omission."""
    monkeypatch.setattr(metrics, "_fetch_series", _fake_metric_series(
        Invocations=series((0, 10.0)),
    ))

    result = metrics.run(incident, target="api")

    assert "Throttles" in result["no_data_for"]
    assert "Throttles" not in result["summary"]


def test_metrics_payload_omits_the_verdict_for_queues(incident, monkeypatch):
    """SQS has no Invocations, so the comparison is meaningless there."""
    monkeypatch.setattr(metrics, "_fetch_series", _fake_metric_series(
        ApproximateNumberOfMessagesVisible=series((0, 4.0), (60, 90.0)),
    ))

    result = metrics.run(incident, target="dlq")

    assert "load_vs_defect" not in result
    assert result["resource"] == metrics.QUEUE_NAMES["dlq"]


def test_metrics_payload_handles_a_window_with_no_data_at_all(incident, monkeypatch):
    monkeypatch.setattr(metrics, "_fetch_series", _fake_metric_series())

    result = metrics.run(incident, target="api")

    assert result["has_data"] is False
    assert result["load_vs_defect"]["verdict"] == "insufficient_data"


def test_metrics_payload_is_bounded(incident, monkeypatch):
    """A full window at 60s granularity must stay small enough that resending it
    every turn is affordable."""
    full = series(*[(i * 60, float(i)) for i in range(11)])
    monkeypatch.setattr(metrics, "_fetch_series", _fake_metric_series(
        Invocations=full, Errors=full, Duration=full,
        Throttles=full, ConcurrentExecutions=full,
    ))

    result = metrics.run(incident, target="api")

    assert len(json.dumps(result)) < 3000


# --------------------------------------------------------------------------- #
# base — window and resource guards
# --------------------------------------------------------------------------- #

def test_window_is_symmetric_around_the_incident(incident):
    start, end = base.window_for(incident)

    assert start == incident["triggered_at"] - Config.LOG_WINDOW_MINUTES * 60
    assert end == incident["triggered_at"] + Config.LOG_WINDOW_MINUTES * 60


def test_window_rejects_an_incident_without_a_timestamp():
    with pytest.raises(base.ToolError, match="triggered_at"):
        base.window_for({"incident_id": "x"})


def test_resolve_log_groups_maps_short_names():
    assert base.resolve_log_groups("api") == ["/aws/lambda/sentry-capstone-api-gulsher"]
    assert base.resolve_log_groups("consumer") == [
        "/aws/lambda/sentry-capstone-consumer-gulsher"
    ]
    assert len(base.resolve_log_groups("both")) == 2


def test_resolve_log_groups_refuses_an_arbitrary_group():
    """This is the boundary that keeps the agent out of other teams' logs — and
    out of its own, which would let it read its own reasoning as evidence."""
    with pytest.raises(base.ToolError):
        base.resolve_log_groups("/aws/lambda/sentry-capstone-agent-gulsher")


def test_every_resolvable_log_group_is_allow_listed():
    for choice in ("api", "consumer", "both"):
        for group in base.resolve_log_groups(choice):
            assert group in Config.TARGET_LOG_GROUPS


def test_agent_can_never_resolve_its_own_log_group():
    for group in Config.TARGET_LOG_GROUPS:
        assert "agent" not in group, "the agent must not be able to read its own logs"
        assert "ingest" not in group


def test_resolve_function_refuses_an_arbitrary_name():
    with pytest.raises(base.ToolError):
        base.resolve_function("some-other-teams-function")


def test_truncate_leaves_short_text_alone():
    assert base.truncate("short") == "short"


def test_truncate_reports_the_original_length():
    result = base.truncate("y" * 900, limit=100)

    assert result.startswith("y" * 100)
    assert "900 chars total" in result


def test_truncate_passes_through_none():
    assert base.truncate(None) is None


def test_error_result_is_shaped_like_a_result():
    """The model reasons over it, so it must not look different from success."""
    result = base.error_result("access denied", log_groups_searched=["a"])

    assert result["error"] == "access denied"
    assert result["result_count"] == 0
    assert result["log_groups_searched"] == ["a"]


# --------------------------------------------------------------------------- #
# runbooks — the negative case is the point
# --------------------------------------------------------------------------- #

def test_runbook_matches_a_documented_failure(incident):
    result = runbooks.run(incident, symptoms="KeyError traceback after a deploy, 500s")

    assert result["matched"] is True
    assert result["runbooks"][0]["id"] == "RB-001"


def test_runbook_no_match_says_so_explicitly(incident):
    """The no-runbook decoy. A weak match stretched to fit is the failure mode."""
    result = runbooks.run(incident, symptoms="the moon is in the wrong phase")

    assert result["matched"] is False
    assert result["result_count"] == 0
    assert "no documented procedure" in result["message"].lower()
    assert "null" in result["message"], "the model must be told what to emit"


def test_runbook_requires_symptoms(incident):
    assert runbooks.run(incident, symptoms="   ")["matched"] is False


def test_runbook_results_are_capped(incident):
    result = runbooks.run(
        incident,
        symptoms="keyerror exception traceback deploy accessdenied permission iam "
                 "throttling capacity dlq dead letter memory oom latency duration timeout",
    )

    assert len(result["runbooks"]) <= runbooks.MAX_RESULTS


def test_runbook_results_are_ranked_by_score(incident):
    result = runbooks.run(incident, symptoms="accessdenied not authorized iam policy")
    scores = [r["match_score"] for r in result["runbooks"]]

    assert scores == sorted(scores, reverse=True)
    assert result["runbooks"][0]["id"] == "RB-002"


def test_every_runbook_suggests_a_valid_remediation():
    """A runbook suggesting a remediation the schema rejects is a guaranteed
    validation failure and a wasted repair round trip."""
    from sentry.agent.schema import VALID_REMEDIATIONS

    for runbook in runbooks.RUNBOOKS:
        assert runbook["remediation"] in VALID_REMEDIATIONS


def test_every_runbook_has_a_unique_id():
    ids = [r["id"] for r in runbooks.RUNBOOKS]
    assert len(ids) == len(set(ids))
