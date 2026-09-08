# Sentry — Autonomous Incident Triage & RCA Agent

AWS GenAI bootcamp capstone. An event-driven agent that diagnoses production
incidents and proposes remediation behind a human approval gate.

**The claim being tested is not that an LLM can read logs.** It is that an agent
can correlate evidence across disconnected sources, resist the obvious-but-wrong
conclusion, and abstain when the evidence does not support an answer. The
evaluation is built around exactly that.

---

## Repository layout

**The application under test lives in a separate repository**
(`Gulsher-19I-0445/Test-app-for-sentry`) and is deployed by hand. Nothing
here imports it; see "Why two repositories" below.

```
src/
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
├── scoring.py               four-axis scoring
└── target_app_contract.py   mirror of the other repo's failure modes
frontend/
├── index.html               operator dashboard + the "how to run" panel
└── preview.html             generated from index.html, every endpoint faked
cli-reference.md             every working command + a gotchas table (gitignored)
```

One deployment bundle here. The same zip goes to every Lambda; only the
handler string differs.

```powershell
cd src; Compress-Archive -Path sentry -DestinationPath ..\sentry.zip -Force; cd ..
```

The application's bundle is built in its own repository, with the same
command and `target_app` in place of `sentry`.

---

## Deployed resources (shared learning account, us-east-1)

| Resource | Name | Handler |
|---|---|---|
| API Lambda* | `sentry-capstone-api-gulsher` | `target_app.api.handler.handler` |
| Consumer Lambda* | `sentry-capstone-consumer-gulsher` | `target_app.consumer.handler.handler` |
| Ingest Lambda | `sentry-capstone-ingest-gulsher` | `sentry.ingest.handler.handler` |
| Agent Lambda | `sentry-capstone-agent-gulsher` | `sentry.agent.handler.handler` |
| App table | `sentry-capstone-app-gulsher` | orders + flags |
| Incidents table | `sentry-capstone-incidents-gulsher` | incident state machine |
| Orders queue | `sentry-capstone-orders-gulsher` (+ `-dlq-`) | |
| Work queue | `sentry-capstone-work-gulsher` (+ `-dlq-`) | |
| Alarms | `sentry-capstone-{api-errors,consumer-errors,api-latency,dlq-depth}-gulsher` | |

\* Deployed from the application's own repository, by hand. They are listed
here because Sentry watches them and the executor may roll their alias back,
not because this repository builds them.

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
comments that reach production output. Two tests in the application's
repository (`tests/test_log_hygiene.py`) fail its build over exactly this.

**Why two repositories.** The agent reads a repository's commits as evidence:
per commit it receives the subject line and the changed file paths
(`changes.py:272,292`). While one repository held both halves, commit `31edd00`
would have handed it *"Fix the adversarial scenarios and check the definitions
offline"* alongside `evals/scenarios.py` — telling it that it is being
evaluated, that the scenarios are adversarial, and where the answers live.

That was prevented only by a rule a human had to remember on every commit, and
breaking it would have been silent and retroactive: you would not know which
run was contaminated. `GITHUB_REPO` now names a repository that physically does
not contain `evals/`, so the leak is impossible rather than merely forbidden.
The same argument as enforcing safety at the IAM boundary instead of in the
prompt, and the same one behind the agent never reading its own log group.

`evals/target_app_contract.py` mirrors the application's failure modes because
the import is gone. Copies drift, so `harness.check_modes_against_deployment()`
asks the deployed application what it actually offers before every sweep.

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

**Built and tested offline:** the target app and its fault injection, the alarm
pipeline, ingest with dedup, the agent and its four tools, the approval gate,
the executor, the operator dashboard, the eval harness and scoring, 441 unit
tests with CI, and a Terraform config describing the whole system.

**Measured:** baseline of 15 investigations across 9 scenarios on 2026-09-03 —
see `EVALUATION.md`. **Zero false attributions across all 15.** S11 — a real
Lambda version published minutes before the alarm, nothing actually wrong —
answered "no change is implicated" three times out of three. Mean $0.129 per
investigation, 7.5 tool calls, 51 seconds.

Every repeated scenario was perfectly consistent: same cause, near-identical
confidence. That resolves the "not reproducible run to run" concern below, at
least for these scenarios.

**Written but never run against AWS:**

- the Terraform config (`terraform validate` passes; `plan` has never run
  against real credentials)

The approval gate and executor **are** deployed. The dashboard now renders live
data and can drive the application itself — see its "How to run" panel — which
leaves Terraform as the only remaining gap between "the code exists" and "the
system works". None of it is covered by the baseline.

**Not started:** the write-up. Terraform now exists but has not replaced the
console-built stack — it deploys a parallel one under a different `owner`.

**The API function currently carries a deliberate defect** (2026-09-07). The
bad-deploy demo needs a genuine one rather than an armed mode, so
`Test-app-for-sentry` commit `9bfc0b6` adds `body["customer"]["tier"]` to
`api/handler.py:91` and it is deployed. Every `POST /orders` raises
`KeyError: 'customer'`, and because the Function URL is unqualified this holds
for `$LATEST` regardless of where the `live` alias points. Push the clean bundle
to `$LATEST` to get a healthy app back. Full runbook, replay mechanism and traps
in `cli-reference.md`, "Bad-deploy demo".

This is the first end-to-end run of the whole chain against a real deploy —
alarm, agent, correct attribution to a real commit *and* the CloudTrail events
behind it, approval, alias rollback. Two observations from it, neither yet
measured properly:

- With `exception` armed *and* the commit present, the agent attributed the
  chaos failure to the innocent commit at 0.90 confidence. That coincidence was
  manufactured — the commit subject was chosen to mirror the mode's `KeyError` —
  so it is not a fair adversarial result. But the discriminator that would have
  caught it was in its context: the trace was in `common/_internal.py`, and the
  commit touched `api/handler.py`.
- Confidence was 0.90 on both that wrong attribution and the correct one. n=2,
  so it is an observation rather than a finding, but calibration is an axis the
  scoring claims to measure and these two did not separate.

---

## Open problems

**Cost per investigation.** Was ~$0.13–0.22; the measured baseline is **$0.129
mean** across 15 investigations (range $0.086–$0.188, 7.5 tool calls, 51s).
A single easy case reached $0.0599, but that is not representative — cost tracks
evidence ambiguity, and ruling candidates out before abstaining is the expensive
part. What moved it:

- log entries deduplicated before returning
- metric series in a dense `{start, period, values[]}` encoding
- the traffic comparison computed in code (`load_vs_defect`)
- CloudTrail filtered to target-app resources only
- routine platform lines excluded in the query rather than after it

The earlier failed experiment is still worth remembering: replacing raw metric
series with summary statistics alone made things *worse* (6 -> 11 calls,
$0.13 -> $0.22), because the model compensated with more tool calls.
Under-informing costs more than over-informing.

**The remaining lever is prompt caching**, not payload shaping. Four tool calls
with one payload each is near the floor. Input is 70% of cost and most of it is
the prefix being resent each turn; Bedrock Converse `cachePoint` blocks price
that at ~0.1x. Estimated landing point ~$0.035.

**Over-escalation — measured, and it splits in two.** On adversarial scenarios,
where evidence genuinely is absent, abstention is 6/6 correct. On genuine
scenarios it is 1/6: the agent identifies the cause and escalates anyway,
because nothing it can see explains what *changed* to trigger the fault. The
chaos flag lives in DynamoDB, invisible to logs, metrics, CloudTrail and git.
That figure measures scenario design, not judgment. See `EVALUATION.md`.

An earlier and separate cause was a real defect: `validate()` rejected an RCA
that both escalates and proposes a remediation, but `SCHEMA_DESCRIPTION` never
stated the rule, so the model could only discover it by failing validation and
paying a repair round trip.

**Rule of thumb this produced:** every rule `validate()` enforces must appear in
`SCHEMA_DESCRIPTION`. A validator that knows something the prompt does not is a
repair round trip you are paying for on every run.

**Test-run contamination — fixed, and it was worse than it looked.** Scenarios
run ~2.5 minutes apart against a ±5 minute evidence window, so every window
reached into its predecessor; two scenarios once diagnosed a third's failure.
The harness now clears the target log groups between scenarios and drains the
dead-letter queues before a sweep. Note metrics cannot be purged — only logs —
so leave a few minutes between sweeps.

**Two scenarios do not test what they claim.** S02 is documented as an
`AccessDenied` test but produces `NoSuchBucket` — the bucket was never created.
S13 is documented as a minimal-evidence test but raises an ordinary
`RuntimeError` with a full traceback, which is why it answers `code_defect`
consistently rather than `unknown`. The agent surfaced the first of these
itself. Both need fixing or relabelling before the next sweep.

**Reproducibility — better than expected.** Sonnet 5 rejects `temperature`, so
run-to-run stability could not be assumed. In practice every repeated scenario
gave the same cause and near-identical confidence across three runs. Still worth
repeating anything before quoting it, but the concern did not materialise.

---

## Testing

**441 unit tests in `tests/`, run with `python -m pytest`.** No AWS credentials,
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
| agent learns the failures are injected | anything that reaches its evidence: a log message, **a commit subject line**, or **a changed file path** — `changes.py:292` fetches paths per commit. File *bodies* are not fetched. Splitting the repositories removes the commit half structurally |
| Lambda loses `INCIDENTS_TABLE` after a config change | `update-function-configuration --environment` **replaces** the whole variable map — read, merge, write, or use the console |
| CI fails before running any test | `actions/setup-python` with `cache: pip` globs for `requirements.txt`/`pyproject.toml` and errors when neither exists — set `cache-dependency-path` |
| two unrelated edits land in one commit | `git add <file>` stages the whole file; splitting them afterwards needs `reset --soft` and a temporary revert |
| e2e run produces no investigation | the sweep pre-flight *disables* alarm actions — an end-to-end test needs them **enabled**, and the alarm still only fires on a transition |
| agent sees no commits at all | the fine-grained PAT is not scoped to `GITHUB_REPO`. GitHub answers **404, not 401**, for a private repo a token cannot see, and `_fetch_commits` turns that into `[]` — indistinguishable from "nothing was committed". Splitting the repositories moved `GITHUB_REPO` without re-scoping the token |
| rotating the GitHub token changes nothing | `_token_cache` is a module global set once per container; a warm agent keeps serving the old token. Force a cold start |
| rollback succeeds but the errors continue | the Function URL is unqualified, so traffic runs `$LATEST` while the executor only moves the alias |

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