# Deploying Sentry

One `apply` gives you the whole system: an application that fails on purpose, a
pipeline that notices, an agent that investigates, and a gate a human approves
through.

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
cp terraform.tfvars.example terraform.tfvars   # set `owner` at minimum
terraform init
terraform plan          # read this before applying
terraform apply
```

Then:

```bash
terraform output -raw approval_url      # paste into frontend/index.html
terraform output -raw approval_token
```

Open `frontend/index.html` in a browser, paste both, and you have the dashboard.

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

## Investigating your own applications

```hcl
create_target_app = false

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
dimensions depend on the service.

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

- **State contains secrets in plaintext** — the generated approval token, the
  demo app's admin token. `*.tfstate` is gitignored; keep it that way, and
  don't paste plan output into a public issue.
- **`terraform.tfvars` is gitignored.** Only the `.example` is committed.
- **The approval Function URL is public** with `authorization_type = NONE`. The
  shared token in the `x-approval-token` header is the only gate, and the
  handler fails closed when it is unset. That stops an accidental request, not
  a determined attacker — put an authorizer in front for anything real.
- **The demo app's endpoint is public too.** It stores nothing sensitive and
  the admin routes are token-gated, but it exists to be broken. Destroy it when
  you are done.

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
