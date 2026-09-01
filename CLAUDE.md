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
    ├── agent/
    │   ├── handler.py       SQS worker: investigate, validate, transition
    │   ├── bedrock.py       Converse API loop, backoff, cost accounting
    │   ├── config.py        all tunables
    │   ├── prompt.py        system prompt + user prompt builder
    │   ├── schema.py        RCA contract + validation
    │   └── tools/
    │       ├── __init__.py  registry: specs() and dispatch()
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
ingest with dedup, agent core with four tools). SEN-21 harness written.

In progress: SEN-22/23 — getting a clean baseline across the genuine scenarios.

Not started: Epic 5 (approval gate + executor), Epic 6 (dashboard),
Epic 7 (adversarial scenarios, calibration, write-up). Terraform translation —
everything is currently console-built, and this must not slip to the final week.

---

## Open problems

**Cost per investigation is too high.** ~$0.13–0.22 on Sonnet. Input tokens
dominate (~90%) because every turn resends all prior tool results. Naive
implementation was $0.24; payload trimming has helped but not enough. Target is
$0.03–0.05.

A first attempt at replacing raw metric series with summary statistics made
things *worse* — the agent compensated by making more tool calls (6 -> 11,
$0.13 -> $0.22). Lesson: over-aggressive summarisation costs more than it saves.
Current thinking is to keep summaries for count metrics but preserve the raw
series for `Duration` and `ErrorRate`, which are the two the model actually
reasons over.

**The agent systematically over-escalates.** It sets
`needs_human_investigation` on cases with clear evidence, so the abstention axis
scores near zero. Partly a scenario problem: the chaos flag lives in DynamoDB,
invisible to CloudTrail and to any diff, so genuinely no discoverable change
explains the failure and escalating is defensible. S01 now publishes a real
Lambda version before arming to give the agent a legitimate rollback target.
Note this is a *safe* failure mode — an over-cautious agent is far better than
one that confidently blames the wrong commit.

**Test-run contamination.** The incident window catches debris from previous
test runs, so the agent finds unrelated failures from other scenarios. Window
narrowed to ±5 minutes; consider deleting the target app log groups between
sweeps.

**GitHub is not configured.** `GITHUB_REPO` and `GITHUB_TOKEN_SECRET` are unset,
so `get_recent_changes` returns CloudTrail deployments without the file paths
that would let the agent connect a change to a stack trace. The agent has twice
named this as the gap preventing a confident conclusion.

---

## Testing

**Unit tests do not exist yet and should.** Every bug so far was catchable
offline: a missing `@dataclass`, a trailing comma making a client a tuple, an
exception class not inheriting from `Exception`, wrong relative import depth, a
`TOOL_SPEC` that did not exist, code changed in one place but not the other.
Each cost a deploy cycle. Highest-value additions:

- import every module; assert `bedrock._client` is a client not a tuple
- assert every registered tool has a matching spec with the right name
- assert every custom exception subclasses `Exception`
- schema validation: valid passes, each semantic rule rejects
- agent loop against a stubbed `_converse` — scripted tool sequences,
  `max_tokens` truncation, invalid-JSON-then-repair

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