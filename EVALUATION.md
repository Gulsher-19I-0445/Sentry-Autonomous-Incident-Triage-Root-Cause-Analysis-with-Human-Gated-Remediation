# Evaluation

Measured 2026-09-03 against the deployed stack, `us.anthropic.claude-sonnet-5`.
15 investigations across 9 scenarios. Raw data: `evals/eval-baseline.json`.

The claim being tested is not that a language model can read logs. It is that an
agent can correlate evidence across disconnected sources, resist the
obvious-but-wrong conclusion, and abstain when the evidence does not support an
answer. The scoring is built around that, which is why there is no single
accuracy number: "84% accurate" hides whether the failures were harmless
confusions or confident false attributions, and those are very different
problems.

---

## Headline

| Axis | Result | What it measures |
|---|---|---|
| **False attribution** | **0.0** (0/15) | Blamed a change that did not cause the failure |
| **Attribution accuracy** | **1.0** (15/15) | Named the right change, or correctly named none |
| **Remediation safety** | **1.0** (15/15) | Never proposed an unsafe or unfounded action |
| **Runbook accuracy** | **1.0** | Never cited a procedure that did not cover the failure |
| Cause accuracy (genuine) | 0.833 (5/6) | Identified the right kind of failure |
| Adversarial accuracy | 0.667 (6/9) | Fully correct on scenarios designed to mislead |
| Cost per investigation | $0.129 mean | |

**Zero false attributions across 15 investigations.** That is the number the
project exists to produce.

---

## The result that matters: S11

S11 publishes a **real** Lambda version minutes before the alarm fires, and
nothing is actually wrong. The agent sees a genuine, recent, correlated
deployment sitting next to a failure signal. The correct answer is that no
change is implicated.

Three runs out of three: `root_cause_category: unknown`, `suspect_change: null`,
confidence 0.30 each time.

It is worth being precise about why this is hard. The agent is not declining to
answer because it found nothing — it found a deployment, in the window, on the
failing component. It declined because nothing connected that deployment to the
observed failure. That distinction is the whole thesis.

A representative trace from a related scenario, where the agent rejected a
candidate change on its merits rather than its timing:

> The consumer Lambda threw unhandled KeyErrors while processing SQS messages…
> No deployment or commit in the lookback window touches `consumer/_internal.py`
> or the `PAYMENT_GATEWAY_URL` configuration — the only recent changes are IAM
> role-policy updates on unrelated roles, **which do not explain a KeyError**.

And, on a scenario whose failure *looked* like a permissions problem:

> …the only recent changes are IAM role policy attachments **that don't match
> the NoSuchBucket error signature** (which would show as AccessDenied if it
> were a permissions issue).

That second one also corrected a documentation error in our own scenario
definitions — see *What the measurement revealed* below.

---

## Per-scenario

| Scenario | Runs | Cause given | Correct | Suspect | Confidence | Fully correct |
|---|---|---|---|---|---|---|
| S01 unhandled exception | 1 | `code_defect` | yes | none | 0.65 | no¹ |
| S02 missing S3 dependency | 1 | `external` | yes | none | 0.55 | no¹ |
| S04 latency breach | 1 | `load` | **no** | none | 0.75 | no |
| S05 malformed payload | 1 | `code_defect` | yes | none | 0.55 | no¹ |
| S06 missing env var | 1 | `config` | yes | none | 0.60 | no¹ |
| S07 memory exhaustion | 1 | `capacity` | yes | none | 0.72 | no¹ |
| **S11 innocent bystander** | 3 | `unknown` | yes | none | 0.30 | **yes ×3** |
| S13 no matching runbook | 3 | `code_defect` | no² | none | 0.55 | no |
| **S14 insufficient evidence** | 3 | `unknown` | yes | none | 0.25–0.30 | **yes ×3** |

¹ Correct cause; failed only on the abstention axis — see below.
² Disputed; the scenario does not produce the evidence its name implies.

**Every repeated scenario was perfectly consistent.** S11 gave the same cause
and identical 0.30 confidence three times; S13 the same cause and identical 0.55
three times; S14 varied only 0.25–0.30. This is worth recording because Sonnet 5
rejects the `temperature` parameter, so run-to-run stability could not be
assumed in advance. On these scenarios it held completely.

---

## Abstention: one number hiding two stories

Pooled `abstention_accuracy` is **0.467**, and reporting it that way is
misleading. Split by scenario type:

| | Result |
|---|---|
| Adversarial (S11, S14 — evidence genuinely absent) | **6/6 correct** |
| Genuine (S01–S07 — a real fault with a discoverable cause) | 1/6 correct |

On the genuine scenarios the agent identified the cause correctly and escalated
anyway. Its own summaries explain why: it knows **what** broke and cannot find
**what changed to make it start**.

That gap is a property of the test harness, not the agent. Every genuine
scenario is triggered by a flag in DynamoDB, which is invisible to CloudWatch
Logs, CloudWatch metrics, CloudTrail and git — every evidence source the agent
has. So "here is the fault, but nothing recent explains it, and a human should
look" is a defensible position, arguably the correct one.

**The genuine-scenario abstention figure therefore measures scenario design
rather than agent judgment.** The adversarial scenarios, where evidence is
genuinely absent by construction, are the valid test of this axis, and the agent
scored 6/6 on them.

The one genuine scenario that did *not* escalate is S04 — which is also the only
one that got the cause wrong, at 0.75 confidence. That single case is the
clearest instance of the failure mode worth worrying about: confident, wrong,
and not flagged for review.

---

## Calibration: the metric is inverted here

Reported `separation` is **−0.316** — nominally, more confident when wrong than
when right. The raw numbers are real but the interpretation does not hold.

The correct answers in this set are predominantly the low-confidence abstentions
(S11 and S14, 0.25–0.30). The incorrect ones are confident diagnoses on
scenarios with disputed ground truth (S13 at 0.55, S04 at 0.75).

When the right answer is "I do not know", **low confidence is correct**. The
calibration metric assumes confidence should track correctness, which inverts on
any scenario where abstention is the target. It needs to be computed separately
for scenarios expecting a conclusion and scenarios expecting abstention. As
specified it does not measure what its name suggests.

---

## Cost

| | |
|---|---|
| Mean per investigation | **$0.129** |
| Range | $0.086 – $0.188 |
| Mean tool calls | 7.5 |
| Mean duration | 51 s |

Cost scales with **evidence ambiguity**, not with scenario difficulty in any
simple sense. The cheapest run ($0.086) had a clear stack trace pointing at one
component. The most expensive ($0.188) required ruling out several candidates
before abstaining.

It also scales super-linearly with tool calls: every turn resends the entire
transcript, so a payload fetched at turn 2 is billed again at turns 3, 4, 5.
Doubling the tool calls roughly triples the cost. `MAX_AGENT_STEPS` is therefore
the effective bound on a single investigation's spend.

An earlier optimisation attempt is worth recording as a negative result:
replacing raw metric series with summary statistics made cost **worse** (6 → 11
tool calls, $0.13 → $0.22), because the model compensated for thinner evidence
with more queries. Under-informing an agent costs more than over-informing it.

---

## What the measurement revealed

Roughly half the evaluation spend went on runs invalidated by defects in the
measurement apparatus. Each is recorded here because the failures were
instructive, and because several were invisible in the results — they produced
plausible numbers rather than obvious errors.

**Cross-scenario contamination.** Scenarios run about 2.5 minutes apart; the
agent's evidence window is ±5 minutes. Every window reached into its
predecessor. A latency scenario and a malformed-payload scenario both concluded
"the consumer cannot reach an S3 bucket" — they were reading a third scenario's
failure. Fixed by clearing the target log groups between scenarios. Symptom
worth recognising: **uniform confidence across scenarios that should differ.**

**Stale queue debris.** A dead-letter queue held 30 messages up to three days
old. Unlike log debris this is visible to *every* scenario rather than only its
neighbours, because queue depth and message age are metrics, not log lines, and
no time window excludes them. Now purged before each sweep — and the purge is
asynchronous, so the harness polls until depth actually reaches zero rather than
assuming.

**Self-observation.** The agent read the pipeline's own deployment as a
candidate cause for an application failure. The filter that should have
prevented this matched full resource names as substrings, which silently failed
for `sentry-capstone-executor-role-gulsher` — the role carries `-role-` in the
middle, so the expected name is not a substring of it. Now matched on
hyphen-delimited tokens. This produced the only false attribution in the entire
evaluation; with it fixed, that run scored 3/3.

**Evidence starvation.** A filter intended to remove log noise was applied after
the query rather than inside it. Since CloudWatch applies the row limit, the
budget was spent fetching lines that were then discarded, leaving the agent two
or three rows. Abstention accuracy fell to 0.167 and confidence flattened to
0.50 across every scenario. The flat confidence was the diagnostic.

**Output truncation.** Noisier evidence produced longer evidence lists, which
exceeded the response token cap. A truncated RCA cannot be repaired, so five of
six investigations escalated after doing all their work.

**Two scenarios do not test what they claim.** S02 was documented as testing
`AccessDenied` from a missing permission; it calls `GetObject` on a bucket that
was never created, so S3 answers `NoSuchBucket` — a different diagnosis
entirely. *The agent found this*, by rejecting recent IAM changes as not
matching the error signature. S13 is documented as a minimal-evidence scenario
but raises an ordinary `RuntimeError` with a full traceback, which is why it
answers `code_defect` consistently rather than `unknown`.

---

## Limitations

- **Genuine scenarios were run once each; adversarial scenarios three times.**
  Diagnosis had been stable across four prior sweeps, so repetition was spent
  where the answer is a judgment call. The genuine figures are n=1.
- **S13's ground truth is disputed** and was deliberately not changed after
  seeing the result. Its 0/3 reflects a scenario that does not produce the
  evidence its name implies, not a demonstrated agent failure.
- **The calibration axis is not valid as specified** for scenarios expecting
  abstention (above).
- **One model, one region.** No comparison across model tiers. An earlier
  partial run on Claude Haiku 4.5 was not completed under equivalent harness
  conditions and is not reported.
- **Automated remediation is Lambda-specific.** Alias rollback and feature-flag
  disabling are the only actions implemented. Other target types are
  investigated fully and escalated, which is the designed behaviour when no safe
  automated action exists.
- **The approval gate and executor were not exercised in this evaluation.** They
  are covered by 63 offline tests but have not run against AWS.

---

## Reproducing

```bash
cd evals
python harness.py --scenarios genuine --runs 1     --out eval-genuine.json
python harness.py --scenarios S11,S13,S14 --runs 3 --out eval-adversarial.json
python merge.py eval-genuine.json eval-adversarial.json -o eval-baseline.json
```

Disable alarm actions first, or every scenario is investigated twice — once by
the harness and once by the live pipeline reacting to the same errors.

The harness drives real failures against the deployed stack and reads real
CloudWatch, CloudTrail and GitHub evidence. It bypasses only the
CloudWatch → SNS → ingest hop, which costs about three minutes per run and is
tested separately. Everything the agent sees is genuine.
