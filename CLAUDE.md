# Sentry — Autonomous Incident Triage & RCA Agent

AWS GenAI bootcamp capstone. An event-driven agent that diagnoses production
incidents and proposes remediation behind a human approval gate.

**The claim being tested is not that an LLM can read logs.** It is that an agent
can correlate evidence across disconnected sources, resist the obvious-but-wrong
conclusion, and abstain when the evidence does not support an answer. The
evaluation is built around exactly that.

---

## Repository layout

```
src/
├── target_app/              app.zip — the app that breaks on purpose
│   ├── api/handler.py       orders API + admin chaos endpoints
│   ├── consumer/handler.py  SQS consumer
│   └── common/
│       ├── _internal.py     fault injection (NEVER name this "chaos" — see below)
│       ├── logging.py       structured JSON + correlation ids
│       └── store.py         DynamoDB: orders + feature flags
│
└── sentry/                  sentry.zip — the triage pipeline
    ├── ingest/handler.py    SNS alarm -> dedup -> work queue
    ├── approval/handler.py  the gate: list, inspect, approve, reject
    ├── executor/handler.py  the ONLY component that mutates anything
    ├── agent/
    │   ├── handler.py       SQS worker: investigate, validate, transition
    │   ├── bedrock.py       Converse API loop, backoff, cost accounting
    │   ├── config.py        all tunables
    │   ├── prompt.py        system prompt + user prompt builder
    │   ├── schema.py        RCA contract + validation
    │   └── tools/
    │       ├── init.py      registry: specs() and dispatch()
    │       │                (NOT __init__.py, which is empty — the handler
    │       │                 imports `from .tools.init import ...`)
    │       ├── base.py      window/resource guards shared by all tools
    │       ├── logs.py      CloudWatch Logs Insights
    │       ├── metrics.py   CloudWatch metrics
    │       ├── changes.py   CloudTrail + GitHub
    │       └── runbooks.py  keyword match over a small corpus
evals/
├── scenarios.py             ground truth
├── harness.py               integration harness (real AWS, real model)
└── scoring.py               four-axis scoring
docs/
├── cli-reference.md         every working command + a gotchas table
├── manual-setup.md          console setup for the target app
└── sen-*-setup.md           per-ticket setup notes
```

Two separate deployment bundles. Same zip goes to every Lambda in its group;
only the handler string differs.

```powershell
cd src; Compress-Archive -Path target_app -DestinationPath ..\app.zip -Force; cd ..
cd src; Compress-Archive -Path sentry     -DestinationPath ..\sentry.zip -Force; cd ..
```

---

## Deployed resources (learning account 043309363336, us-east-1)

| Resource | Name | Handler |
|---|---|---|
| API Lambda | `sentry-capstone-api-gulsher` | `target_app.api.handler.handler` |
| Consumer Lambda | `sentry-capstone-consumer-gulsher` | `target_app.consumer.handler.handler` |
| Ingest Lambda | `sentry-capstone-ingest-gulsher` | `sentry.ingest.handler.handler` |
| Agent Lambda | `sentry-capstone-agent-gulsher` | `sentry.agent.handler.handler` |
| App table | `sentry-capstone-app-gulsher` | orders + flags |
| Incidents table | `sentry-capstone-incidents-gulsher` | incident state machine |
| Orders queue | `sentry-capstone-orders-gulsher` (+ `-dlq-`) | |
| Work queue | `sentry-capstone-work-gulsher` (+ `-dlq-`) | |
| Alarms | `sentry-capstone-{api-errors,consumer-errors,api-latency,dlq-depth}-gulsher` | |

Naming convention: `sentry-capstone-<component>-gulsher`. This is a **shared
account** with other engineers' workloads — never create anything without that
prefix, and never query outside it.

Bedrock: `us.anthropic.claude-sonnet-5` (agent),
`us.anthropic.claude-haiku-4-5-20251001-v1:0` (eval runs). Bare model ids do
not work — the `us.` inference profile prefix is required.

---

## Flow

```
alarm -> SNS -> ingest (dedup by alarm+5min bucket) -> SQS work queue
      -> agent (4 read-only tools -> RCA) -> DynamoDB
      -> approval gate -> executor (alias shift | feature flag)
```

Incident status machine, enforced atomically in DynamoDB:
`NEW -> INVESTIGATING -> {PENDING_APPROVAL | ESCALATED | INFORMATIONAL | FAILED}
-> {APPROVED | REJECTED} -> EXECUTED -> CLOSED`

---

## Non-negotiable design rules

**The agent has no write permissions to anything it investigates.** Not to the
target app, not to IAM, not to aliases. Safety is enforced at the IAM boundary,
not by prompt instruction, so a prompt injection or reasoning failure cannot
cause a mutation. Do not add write permissions to the agent role.

**Never let the agent see that failures are injected.** The fault-injection
module is `_internal.py` with functions `_process_request` and `_apply`
specifically so stack traces look ordinary. An earlier version was called
`chaos.py`, the agent read it in a stack trace, and correctly reported "a
fault-injection harness appears to be enabled" — which invalidated the whole
evaluation. Do not reintroduce revealing names in code, log messages, or
comments that reach production output.

**Scope every query to this project.** `Config.TARGET_LOG_GROUPS` and
`TARGET_FUNCTIONS` are allow-lists; tools resolve short names (`"api"`,
`"consumer"`) against them rather than accepting arbitrary resource names.
CloudWatch metrics and CloudTrail have no resource-level IAM, so filtering
happens in code — see `changes._relevant()`.

**The agent must never read its own log group.** Reading its own prior reasoning
as evidence is a feedback loop that is very hard to spot.

**Abstention is a first-class output, not a fallback.** `unknown` +
`needs_human_investigation` is a correct answer when evidence is insufficient,
and it is scored as such. Never "improve" the prompt in a way that pressures the
model to always conclude.

**Chaos is armed, not fired directly.** You arm a mode, then ordinary traffic
triggers it, so the incident looks like a real production failure rather than
someone hitting a `/break` endpoint.

---

## Where it stands

Done: Epics 0–3 (environment, target app, chaos injection, alarm pipeline,
ingest with dedup, agent core with four tools). SEN-21 harness written. Unit
test suite + CI. GitHub wired into `get_recent_changes`.

In progress: SEN-22/23 — getting a clean baseline across the genuine scenarios.
A full end-to-end run on 2026-09-02 diagnosed a real code-caused failure
correctly (4 tool calls, $0.0599, confidence 0.92, commit-level attribution,
first `PENDING_APPROVAL`). That is one run on one scenario — the sweep has not
been done.

Epic 5 written but **not deployed**: `approval/` and `executor/` exist with 63
tests. Neither has ever run against AWS, and neither has a Lambda, a role, or a
URL yet. Until they are deployed, every `PENDING_APPROVAL` incident is still a
dead end.

Not started: Epic 6 (dashboard), Epic 7 (adversarial scenarios, calibration,
write-up). Terraform translation — everything is currently console-built, and
this must not slip to the final week.

**Blocking the sweep:** `app.zip` currently carries a deliberate defect
(`body["customer_tier"]` in `_create_order`, commit `ac5dec34`) so that a
code-caused failure could be tested end to end. Every order request 500s
regardless of chaos mode, so no genuine scenario can run until it is reverted
and redeployed.

---

## Open problems

**Cost per investigation.** Was ~$0.13–0.22; now **$0.0599** (4 tool calls,
14,055 input / 1,181 output) against a $0.03–0.05 target. What actually moved it:

- log entries deduplicated before returning (identical stack traces were resent
  in full on every turn)
- metric series in a dense `{start, period, values[]}` encoding instead of a
  list of `{timestamp, value}` objects
- the traffic comparison computed in code (`load_vs_defect`) so the model does
  not spend a turn deriving it
- CloudTrail filtered to target-app resources only — the pipeline's own deploys
  were 74% of the change payload during a sweep

The earlier failed experiment is still worth remembering: replacing raw metric
series with summary statistics alone made things *worse* (6 -> 11 calls,
$0.13 -> $0.22), because the model compensated with more tool calls.
Under-informing costs more than over-informing.

**The remaining lever is prompt caching**, not payload shaping. Four tool calls
with one payload each is near the floor. Input is 70% of cost and most of it is
the prefix being resent each turn; Bedrock Converse `cachePoint` blocks price
that at ~0.1x. Estimated landing point ~$0.035.

**Over-escalation — largely resolved, and the cause was not the model.**
`validate()` rejected an RCA that both escalates and proposes a remediation, but
`SCHEMA_DESCRIPTION` never stated that rule, so the model could only discover it
by failing validation and paying a repair round trip. The field name compounded
it: `needs_human_investigation` reads as "should a human review this", which in
a human-gated system is always true. Both are now stated explicitly in the
prompt. Confidence went 0.4 -> 0.82 -> 0.92 across successive runs.

**Rule of thumb this produced:** every rule `validate()` enforces must appear in
`SCHEMA_DESCRIPTION`. A validator that knows something the prompt does not is a
repair round trip you are paying for on every run.

**Test-run contamination.** The incident window catches debris from previous
test runs, so the agent finds unrelated failures from other scenarios. Window
narrowed to ±5 minutes; consider deleting the target app log groups between
sweeps.

**Abstention is now untested under the new conditions.** With GitHub live, every
genuine scenario becomes a false-attribution test: the agent sees recent commits
touching the failing file while a DynamoDB flag is the real cause. Correctly
answering "no change is implicated" there is the harder half of the thesis and
has never been run with commits visible.

**Results are not reproducible run to run.** Sonnet 5 rejects `temperature`, so
every number above is n=1. Run three times before quoting anything.

---

## Testing

**321 unit tests in `tests/`, run with `python -m pytest`.** No AWS credentials,
no network — `conftest.py` overrides credentials with fakes and blocks
`socket.connect`, so a test that escapes stubbing fails loudly instead of
quietly calling AWS. CI runs them on push (`.github/workflows/tests.yml`).

They are discovery-based rather than hardcoded: modules are enumerated with
`pkgutil`, exception classes are found by AST-scanning every `raise`, so new
code is covered without anyone remembering to update the suite. Coverage:

- import every module; assert no boto3 client is a tuple (the trailing-comma bug)
- every registered tool has a spec whose name matches its registry key, and a
  callable whose signature accepts every advertised parameter
- every class that gets raised actually subclasses `Exception`
- schema validation: valid passes, each semantic rule rejects with a message
  specific enough for the model to repair itself
- the Converse loop against a stubbed `_converse` — scripted tool sequences,
  token accumulation, `max_tokens` truncation, exactly-one-repair
- tool payload shaping: dedup, dense series encoding, the load-vs-defect
  verdict, CloudTrail relevance filtering
- the target app never reveals fault injection in a log message or symbol name

Two bugs were found by writing them: `IllegalTransition` did not subclass
`Exception`, and `logs.run` computed a deduplication then returned the raw rows
anyway.

**The integration harness is not a unit test.** It drives the real deployed
stack with real failures and costs real money (~$0.15/scenario). Use it to
measure diagnosis quality, not to check that code imports.

It deliberately bypasses the alarm→SNS→ingest hop (3 minutes per run) and
creates the incident directly. That hop is tested separately in SEN-12.

Before any sweep:
```powershell
aws cloudwatch disable-alarm-actions --alarm-names sentry-capstone-api-errors-gulsher `
  sentry-capstone-consumer-errors-gulsher sentry-capstone-api-latency-gulsher `
  sentry-capstone-dlq-depth-gulsher
```
Otherwise every scenario is investigated twice — once by the harness, once by
the real pipeline reacting to the same errors.

---

## Gotchas already paid for

| Symptom | Cause |
|---|---|
| `No module named 'lambda_function'` | handler left at the console default |
| `KeyError: 'TABLE_NAME'` | env var missing; read at import time, so it fails at cold start |
| relative import errors | missing `__init__.py`, or zip root is `src/` not the package |
| `ResourceConflictException` on publish | function still updating — `aws lambda wait function-updated` |
| `'tuple' object has no attribute 'converse'` | trailing comma after `boto3.client(...)` |
| `Float types are not supported` | DynamoDB rejects floats — convert to `Decimal(str(x))` |
| `temperature is deprecated for this model` | Sonnet 5 does not accept it; results are therefore not reproducible run-to-run |
| `on-demand throughput isn't supported` | use the `us.` inference profile id |
| agent invoked twice for one incident | botocore's 60s read timeout retried; set `read_timeout` and `max_attempts=1` |
| incident stuck in `INVESTIGATING` | crashed mid-run; the status guard means it is never retried |
| alarm will not fire | it only publishes on a state *transition* — force OK then ALARM |
| JSON args rejected by the CLI | PowerShell passes backslashes literally — use a here-string or `ConvertTo-Json` |
| GitHub commit window silently matches nothing | `datetime.isoformat()` renders UTC as `+00:00`, and a bare `+` decodes to a space in a query string — encode the params, or use the `Z` form |
| agent learns the failures are injected | anything that reaches its evidence: a log message, **a commit subject line**, or repo file *contents* if it ever gets read access. Paths and file bodies are not fetched today; messages are |
| Lambda loses `INCIDENTS_TABLE` after a config change | `update-function-configuration --environment` **replaces** the whole variable map — read, merge, write, or use the console |
| CI fails before running any test | `actions/setup-python` with `cache: pip` globs for `requirements.txt`/`pyproject.toml` and errors when neither exists — set `cache-dependency-path` |
| two unrelated edits land in one commit | `git add <file>` stages the whole file; splitting them afterwards needs `reset --soft` and a temporary revert |
| e2e run produces no investigation | the sweep pre-flight *disables* alarm actions — an end-to-end test needs them **enabled**, and the alarm still only fires on a transition |

---

## Conventions

- Python 3.12+ style, stdlib only in Lambda (boto3 is in the runtime).
  No pydantic — `pydantic-core` is a compiled wheel and cross-building it for
  Lambda from Windows is not worth it for one model.
- Structured JSON logging everywhere, always with the correlation id.
- Tools return dicts and never raise for expected conditions — "no results" and
  "access denied" are evidence the model should reason about, not crashes.
- Comments explain *why*, especially where a choice looks odd (the dedup
  conditional write, the `_relevant` filter, the abstention rules).
- Config values live in `config.py`, read from env. Nothing hardcoded in handlers.