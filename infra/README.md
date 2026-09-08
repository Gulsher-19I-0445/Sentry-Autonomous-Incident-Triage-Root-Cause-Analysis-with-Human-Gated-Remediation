# Deploying Sentry

One `apply` gives you Sentry: a pipeline that notices an alarm, an agent that
investigates it, and a gate a human approves through. It deploys none of the
applications it watches — what it watches is software you already run, listed
in `investigation_targets`.

The demo application used to develop and evaluate this lives in its own
repository and is deployed separately. Keeping it out is not tidiness: the
agent reads a repository's commit subjects and changed file paths as evidence,
so the repository it reads must not also contain the evaluation's ground
truth.

Nothing here is specific to the account it was written in. Every name is built
from `project_name` and `owner`, so two people can deploy into the same account
without colliding.

## Prerequisites

- Terraform >= 1.5, AWS credentials with permission to create Lambda, IAM,
  DynamoDB, SQS, SNS, CloudWatch and Secrets Manager resources
- **Bedrock model access granted** for the model in `agent_model_id`, in the
  region you are deploying to. This cannot be requested through Terraform —
  do it in the Bedrock console under *Model access*. Without it every
  investigation fails with `AccessDeniedException`.

## Deploy

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars   # set `owner` and investigation_targets
terraform init
terraform plan          # read this before applying
terraform apply
```

`investigation_targets` is required. An apply with none fails at plan time
rather than deploying something inert.

Then wire up the dashboard:

```bash
cp frontend/config.example.json frontend/config.json

terraform output -raw approval_url    # -> config.json
terraform output -raw approval_token  # paste into the page
```

`config.json` holds only endpoint URLs, so it is safe to deploy next to the
page; tokens stay out of it and are pasted by whoever opens it. Open
`frontend/index.html` and you have the dashboard. The page works without
`config.json` too — there is just nothing prefilled.

The application being watched is deployed separately, so its URL and admin
token are not outputs here. Supply them and **"How to run"** in the header
becomes a control surface: pick a failure mode, arm it, send traffic, and watch
the incident arrive, with no AWS credentials and no terminal. Leave them out
and the dashboard still works, without the run controls.

`frontend/preview.html` is the same page with every endpoint faked. Share that
when someone should see the system without being handed real tokens.

## The GitHub token is deliberately not managed here

Setting `github_repo` lets the agent read recent commits as evidence — which is
what allows it to attribute a failure to a specific change rather than just a
deployment window.

Terraform creates the secret **empty**. Populate it yourself:

```bash
aws secretsmanager put-secret-value \
  --secret-id "$(terraform output -raw github_secret_name)" \
  --secret-string '{"token":"github_pat_..."}'
```

Passing the token through Terraform would put it in the plan output, the state
file, and any CI log that echoes either. The agent degrades gracefully while it
is unset — the commits path logs `github not configured` and returns nothing
rather than failing.

Use a fine-grained PAT with **contents: read** on one repository. Nothing more.

## Choosing what to investigate

```hcl
investigation_targets = [
  {
    name                 = "checkout"
    log_group            = "/aws/lambda/checkout-prod"
    lambda_function_name = "checkout-prod"   # optional
  },
  {
    name      = "orders-api"
    log_group = "/ecs/orders-api"
  },
]
```

Anything that writes to CloudWatch Logs and emits CloudWatch metrics can be
investigated — Lambda, ECS, EKS via Container Insights, EC2 with the agent.

`lambda_function_name` is what makes automated rollback possible. Targets
without it are still investigated fully; the agent proposes no automated
remediation and escalates to a human instead. That is the designed behaviour
when the system cannot act safely, not a degraded mode.

Alarms are created automatically for Lambda targets. For anything else, define
your own and point them at the `alarms` SNS topic — the metric names and
dimensions depend on the service. `dlq_queue_name` adds a queue-depth alarm,
which is often the only signal that a consumer is failing silently.

There are **two** topics, and picking the wrong one matters. `alarms` is the
pipeline's input: ingest subscribes to it, so everything published there
becomes an incident the agent investigates. `notify` reaches a human and
nothing else, and is where the agent's own failure alarm goes — routing that
one to `alarms` would have the agent investigate its own crashes. Setting
`alarm_email` subscribes the address to both, so expect two confirmation
emails.

`app_flag_table` names a DynamoDB table whose feature flags the executor may
turn **off**, never on. Leave it unset and that remediation is withheld
entirely: the agent escalates to a human instead, which is the designed
behaviour when the system cannot act safely.

## What the IAM boundary is doing

The safety argument is enforced here, not in the prompt:

| role | may |
|---|---|
| `agent` | read logs from the target log groups, read metrics, read CloudTrail, invoke Bedrock, write **only** to its own incident record |
| `executor` | shift an alias on an enumerated target function, disable a feature flag |
| `approval` | transition an incident and invoke the executor — **no** write permission of its own |

The agent holds no mutating permission on anything it investigates, so a prompt
injection or a reasoning failure can produce a wrong *proposal* but not a wrong
*action*. It also cannot read its own log group, which would let it consume its
previous reasoning as fresh evidence.

`terraform plan` is a good way to see this: read the three policy documents and
check that nothing in the agent's grants any verb that changes state.

## Security notes

- **State contains the generated approval token in plaintext.** `*.tfstate` is
  gitignored; keep it that way, and don't paste plan output into a public
  issue.
- **`terraform.tfvars` is gitignored.** Only the `.example` is committed.
- **The approval Function URL is public** with `authorization_type = NONE`. The
  shared token in the `x-approval-token` header is the only gate, and the
  handler fails closed when it is unset. That stops an accidental request, not
  a determined attacker — put an authorizer in front for anything real.
- **There is no rate limit on the approval URL.** Fine for a link shared with a
  few people, which is what this assumes. Each investigation costs real money —
  budget roughly $0.13 a time. If the dashboard is going somewhere genuinely
  public, put API Gateway with a usage plan in front rather than relying on the
  token alone.
- **The application being watched is deployed separately** and has its own
  exposure to reason about. If it carries a Function URL so the dashboard can
  drive it, that endpoint is public; see that repository's README.

## Cost

Near zero at rest — everything is pay-per-request and idle. The cost is
Bedrock: roughly **$0.06–0.20 per investigation** depending on how ambiguous
the evidence is. Cheap when a clear cause exists; more when the agent has to
rule things out before it can justify abstaining.

`max_agent_steps` is the hard ceiling on tool-use turns and therefore the
bound on a single investigation's cost.

## Teardown

```bash
terraform destroy
```

CloudWatch log groups are managed here, so they go too. The GitHub secret is
created with a zero-day recovery window so the name is immediately reusable —
which also means destroying it is immediate and irreversible.
