# Sentry — autonomous incident triage and RCA, behind a human gate

An event-driven agent that diagnoses production incidents and proposes
remediation, where a person approves before anything changes.

The claim being tested is not that a language model can read logs. It is that an
agent can correlate evidence across disconnected sources, resist the
obvious-but-wrong conclusion, and **abstain when the evidence does not support an
answer**. The evaluation is built around exactly that, which is why there is no
single accuracy number — see [EVALUATION.md](EVALUATION.md).

Measured over 15 investigations across 9 scenarios: **zero false attributions**,
$0.129 mean cost, 51 seconds. The scenario that matters most publishes a real
deployment minutes before an alarm with nothing actually wrong; the agent
answered "no change is implicated" three times out of three.

```
alarm -> SNS -> ingest (dedup) -> queue -> agent (4 read-only tools)
      -> RCA -> approval gate -> executor
```

## Layout

```
src/sentry/
├── ingest/      SNS alarm -> dedup -> work queue
├── agent/       the investigator: Converse loop, prompt, schema, four tools
├── approval/    the gate — list, inspect, approve, reject
└── executor/    the only component that mutates anything
evals/           scenarios (ground truth), harness, four-axis scoring
frontend/        operator dashboard, and a preview build with faked endpoints
infra/           Terraform for everything above
tests/           441 tests, no credentials, no network
```

## Safety is structural, not prompted

The agent holds **no write permission on anything it investigates**. That is
enforced at the IAM boundary, so a prompt injection or a reasoning failure
produces a wrong *proposal* and never a wrong *action*. Approving and acting are
separate functions with separate roles: the gate can authorise, the executor can
perform, neither can do both alone.

The agent also cannot read its own log group — consuming its own prior reasoning
as fresh evidence is a feedback loop that is very hard to spot.

**Abstention is a first-class output.** `unknown` plus
`needs_human_investigation` is a correct answer when evidence is insufficient,
and it is scored as one.

## The application it watches is a separate repository

Sentry deploys none of the applications it investigates; they are listed in
`investigation_targets`. The demo application used to develop and evaluate it
lives in its own repository and is deployed separately.

That separation is not tidiness. The agent reads a repository's commit subjects
and changed file paths as evidence, so the repository it reads must not also
contain the evaluation's ground truth. While both lived together, a commit
message about fixing the adversarial scenarios would have been handed to the
agent as evidence, alongside the path of the file holding the answers. It was
prevented only by a rule a human had to remember, and breaking it would have
been silent. Now it is impossible rather than forbidden.

## Getting started

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars   # set owner and investigation_targets
terraform init && terraform plan && terraform apply
```

Then point the dashboard at it — see [infra/README.md](infra/README.md). Open
`frontend/preview.html` first if you just want to see what it looks like; every
endpoint in that build is faked, so it needs nothing deployed.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

No AWS credentials and no network: `conftest.py` overrides credentials with
fakes and blocks `socket.connect`, so a test that escapes stubbing fails loudly
rather than quietly calling AWS. The suite is discovery-based rather than
hardcoded — modules are enumerated with `pkgutil` and exception classes found by
AST-scanning every `raise`, so new code is covered without anyone remembering to
update it.

The integration harness in `evals/` is **not** a unit test. It drives the real
deployed stack with real failures and costs real money, roughly $0.15 a
scenario. Use it to measure diagnosis quality, not to check that code imports.
