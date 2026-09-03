"""SEN-21 — evaluation harness.

Runs scenarios end to end against the real deployed stack and scores the RCA
against known ground truth.

DESIGN DECISION — the alarm is bypassed. Waiting for a real CloudWatch alarm
costs ~3 minutes per run, so a 10-scenario x 3-run sweep would take two hours.
Instead the harness drives the real failure, waits for log ingestion, then
creates the incident directly and invokes the agent synchronously.

Everything the agent sees is still real: real logs from a real failure, real
metrics, real CloudTrail. Only the CloudWatch->SNS->ingest hop is skipped, and
that hop is already tested separately in SEN-12.

Usage:
    python harness.py --runs 3
    python harness.py --scenarios S01,S05 --runs 5
    python harness.py --model us.anthropic.claude-haiku-4-5-20251001-v1:0
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import boto3

import scenarios as sc
from scoring import Result, failed, score, summarize, variance_by_scenario
from botocore.config import Config as BotoConfig


REGION = "us-east-1"
API_FN = "sentry-capstone-api-gulsher"
AGENT_FN = "sentry-capstone-agent-gulsher"
TABLE = "sentry-capstone-incidents-gulsher"
# Gates the endpoints that arm a failure mode on the target app. Read from
# the environment so a real token is never written down here: Terraform
# generates one per deployment (terraform output -raw target_admin_token).
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "devtoken123")
_logs = boto3.client("logs", region_name=REGION)
INGESTION_WAIT_S = 25      # CloudWatch logs lag; the agent retries once too
SETTLE_S = 3

_lambda = boto3.client("lambda", region_name=REGION,
                       config=BotoConfig(retries={"max_attempts": 1, "mode": "standard"},
                                         read_timeout=180))
_ddb = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


# --------------------------------------------------------------------------- #
# driving the target app
# --------------------------------------------------------------------------- #

def _consumer_errors_since(start: int) -> int:
    """Did the consumer fail since `start`?

    Deliberately broader than `level = 'ERROR'`. A memory kill produces no
    application log line at all — the runtime is killed before the handler can
    log anything — so a JSON-level filter reports S07 as "did not reproduce"
    every single time, no matter how thoroughly it reproduced. Lambda still
    writes its own platform lines for kills and timeouts, so match those too.
    """
    q = _logs.start_query(
        logGroupNames=["/aws/lambda/sentry-capstone-consumer-gulsher"],
        startTime=start, endTime=int(time.time()),
        # Insights `like /../` is case-SENSITIVE, and Lambda reports a kill as
        # "Status: error\tError Type: Runtime.OutOfMemory" on the REPORT line —
        # neither "Status: error" nor "Error Type" matches an /ERROR/ pattern.
        # That is why the previous broadened filter still found nothing while
        # four invocations were being killed.
        queryString=(
            "fields @timestamp, @message "
            "| filter @message like /ERROR|Runtime.OutOfMemory|Task timed out|"
            "Runtime exited|Status: error/ "
            "| limit 20"
        ),
    )["queryId"]
    for _ in range(15):
        time.sleep(1)
        r = _logs.get_query_results(queryId=q)
        if r["status"] not in ("Running", "Scheduled"):
            return len(r.get("results", []))
    return 0

def _invoke_api(payload: dict) -> dict:
    resp = _lambda.invoke(
        FunctionName=API_FN,
        Payload=json.dumps(payload).encode(),
    )
    body = json.loads(resp["Payload"].read() or b"{}")
    return {"function_error": resp.get("FunctionError"), "body": body}


def _admin(method: str, path: str, body: dict | None = None) -> dict:
    return _invoke_api({
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "headers": {"x-admin-token": ADMIN_TOKEN},
        "body": json.dumps(body) if body else None,
    })


def arm(mode: str, remaining: int) -> None:
    _admin("POST", f"/admin/chaos/{mode}", {"remaining": remaining, "ttl_seconds": 600})


def disarm_all() -> None:
    for mode in ["exception", "slow", "memory", "timeout", "denied",
                 "bad_payload", "missing_env", "retry_storm", "silent"]:
        try:
            _admin("DELETE", f"/admin/chaos/{mode}")
        except Exception:
            pass


def drive_traffic(count: int, elapsed: list[float] | None = None) -> int:
    failures = 0
    for _ in range(count):
        t0 = time.time()
        out = _invoke_api({
            "requestContext": {"http": {"method": "POST"}},
            "rawPath": "/orders",
            "body": json.dumps({"item": "widget", "quantity": 1}),
        })
        if elapsed is not None:
            elapsed.append(time.time() - t0)
        if out["function_error"]:
            failures += 1
        time.sleep(0.3)
    return failures


def publish_and_alias() -> str:
    """Create a real deploy event immediately before the failure, so CloudTrail
    shows a change that correlates — the bad-deploy scenario."""
    _lambda.update_function_configuration(
        FunctionName=API_FN,
        Description=f"eval deploy {int(time.time())}",   # forces a new version
    )
    _lambda.get_waiter("function_updated").wait(FunctionName=API_FN)
    version = _lambda.publish_version(FunctionName=API_FN)["Version"]
    _lambda.update_alias(FunctionName=API_FN, Name="live", FunctionVersion=version)
    return version

# --------------------------------------------------------------------------- #
# driving the agent
# --------------------------------------------------------------------------- #

def create_incident(scenario: sc.Scenario, triggered_at: int) -> str:
    """Write a NEW incident shaped exactly as ingest would have written it."""
    incident_id = f"eval-{scenario.id.lower()}-{triggered_at}"
    _ddb.put_item(Item={
        "pk": f"INCIDENT#{incident_id}",
        "incident_id": incident_id,
        "status": "NEW",
        "alarm_name": scenario.alarm,
        "state_reason": (
            f"Threshold Crossed: 1 datapoint was greater than the threshold "
            f"({datetime.fromtimestamp(triggered_at, tz=timezone.utc).isoformat()})"
        ),
        "metric_name": "Errors",
        "namespace": "AWS/Lambda",
        "dimensions": [],
        "triggered_at": triggered_at,
        "created_at": triggered_at,
        "updated_at": triggered_at,
        "suppressed_count": 0,
        "eval_run": True,          # so these are easy to find and purge
        "ttl": triggered_at + 7 * 86400,
    })
    return incident_id


def run_agent_sync(incident_id: str) -> None:
    """Invoke the agent with an SQS-shaped event, synchronously."""
    event = {"Records": [{
        "messageId": f"eval-{incident_id}",
        "body": json.dumps({"incident_id": incident_id}),
    }]}
    resp = _lambda.invoke(
        FunctionName=AGENT_FN,
        Payload=json.dumps(event).encode(),
    )
    payload = json.loads(resp["Payload"].read() or b"{}")
    if resp.get("FunctionError"):
            raise RuntimeError(f"agent crashed: {payload}")
    if payload.get("batchItemFailures"):
        raise RuntimeError("agent reported batch failure — check its logs")


def read_incident(incident_id: str) -> dict:
    item = _ddb.get_item(Key={"pk": f"INCIDENT#{incident_id}"}).get("Item")
    if not item:
        raise RuntimeError(f"incident {incident_id} vanished")
    return json.loads(json.dumps(item, default=str))


# --------------------------------------------------------------------------- #
# one run
# --------------------------------------------------------------------------- #

def run_scenario(scenario: sc.Scenario, run_index: int, verbose: bool = True) -> Result:
    label = f"{scenario.id} run {run_index + 1}"
    try:
        disarm_all()
        time.sleep(SETTLE_S)

        if scenario.publish_version_first:
            version = publish_and_alias()
            time.sleep(5)   # let CloudTrail record it

        if scenario.chaos_mode:
            arm(scenario.chaos_mode, remaining=scenario.traffic_count + 2)

        # Drive traffic ONCE. There used to be a second, earlier call whose
        # result was immediately overwritten; it burned most of the armed budget
        # (arm() allows traffic_count + 2 fires) so only a couple of requests in
        # the measured batch actually tripped the fault, and any consumer errors
        # it caused happened before `started_at` and so were never counted.
        started_at = int(time.time())
        elapsed: list[float] = []
        failures = drive_traffic(scenario.traffic_count, elapsed) if scenario.traffic_count else 0

        # S09 needs messages to exhaust their retries before reaching the DLQ
        wait_s = 60 if scenario.chaos_mode == "retry_storm" else INGESTION_WAIT_S
        if verbose:
            print(f"  {label}: {failures} API failures driven, waiting {wait_s}s")
        time.sleep(wait_s)

        # The reproduce check depends on WHERE the scenario fails. Several
        # scenarios return a clean 201 from the API by design — S05 in
        # particular is only interesting because the producer succeeds and the
        # consumer breaks.
        if scenario.chaos_mode:
            if scenario.fails_in == "api":
                observed = f"{failures} of {scenario.traffic_count} API calls failed"
                reproduced = failures > 0
            elif scenario.fails_in == "consumer":
                consumer_errors = _consumer_errors_since(started_at)
                observed = f"{consumer_errors} consumer error lines"
                reproduced = consumer_errors > 0
            else:  # latency
                slowest = max(elapsed, default=0)
                observed = f"slowest request {slowest:.1f}s, threshold 3.0s"
                reproduced = slowest > 3.0

            if not reproduced:
                # Report what was actually observed. "did not reproduce" alone
                # costs a whole sweep to work out whether the fault never fired,
                # fired somewhere else, or fired and went undetected.
                return failed(scenario, run_index,
                              f"did not reproduce in {scenario.fails_in} "
                              f"({observed}; mode={scenario.chaos_mode})")

        # if scenario.chaos_mode and failures == 0:
        #     return failed(scenario, run_index,
        #                   "scenario did not reproduce: no invocation failed")

        # if verbose:
        #     print(f"  {label}: {failures} failures driven, waiting for log ingestion")
        time.sleep(INGESTION_WAIT_S)

        triggered_at = int(time.time())
        incident_id = create_incident(scenario, triggered_at)
        run_agent_sync(incident_id)

        incident = read_incident(incident_id)
        rca = incident.get("rca")
        if not rca:
            return failed(scenario, run_index,
                          f"no RCA produced; status={incident.get('status')}")

        result = score(scenario, rca, incident.get("trace") or {}, run_index)

        if verbose:
            mark = "PASS" if result.fully_correct else "FAIL"
            print(f"  {label}: {mark}  cause={result.actual_cause} "
                  f"suspect={result.actual_suspect} conf={result.confidence} "
                  f"tools={len(result.tool_calls)} ${result.cost_usd:.4f}")
        return result

    except Exception as exc:
        if verbose:
            print(f"  {label}: ERROR {exc}")
        return failed(scenario, run_index, f"{type(exc).__name__}: {exc}")
    finally:
        disarm_all()


# --------------------------------------------------------------------------- #
# sweep
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1,
                        help="runs per scenario; >1 measures variance")
    parser.add_argument("--scenarios", type=str, default="genuine",
                        help="'genuine', 'adversarial', 'all', or S01,S05")
    parser.add_argument("--out", type=str, default="eval-results.json")
    args = parser.parse_args()

    if args.scenarios == "genuine":
        selected = sc.GENUINE
    elif args.scenarios == "adversarial":
        selected = sc.ADVERSARIAL
    elif args.scenarios == "all":
        selected = sc.ALL
    else:
        selected = [sc.by_id(s.strip()) for s in args.scenarios.split(",")]

    print(f"Running {len(selected)} scenarios x {args.runs} runs\n")
    started = time.time()
    results: list[Result] = []

    for scenario in selected:
        print(f"{scenario.id}  {scenario.name}")
        for i in range(args.runs):
            results.append(run_scenario(scenario, i))
        print()

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runs_per_scenario": args.runs,
        "elapsed_minutes": round((time.time() - started) / 60, 1),
        "summary": summarize(results),
        "by_scenario": variance_by_scenario(results),
        "results": [r.__dict__ for r in results],
    }

    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=2)

    print("=" * 60)
    summary = report["summary"]
    if "error" in summary:
        print(f"  {summary['error']} (attempted: {summary['attempted']})")
        print("\n  failures:")
        for r in results:
            if not r.ok:
                print(f"    {r.scenario_id} run {r.run_index + 1}: {r.error}")
    else:
        for key, value in summary.items():
            if key != "calibration":
                print(f"  {key:28} {value}")
        print("\n  calibration:")
        for key, value in summary["calibration"].items():
            print(f"    {key:26} {value}")
        print(f"\nwritten to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())