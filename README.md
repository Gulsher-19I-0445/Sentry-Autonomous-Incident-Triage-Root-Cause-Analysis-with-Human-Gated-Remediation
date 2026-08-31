sentry/
├── infra/
│   ├── main.tf              # provider, backend, locals
│   ├── target_app.tf        # SEN-7: API/consumer Lambdas, DynamoDB, SQS
│   ├── pipeline.tf          # SEN-11-13: alarms, SNS, ingest, work queue, DLQ
│   ├── agent.tf             # SEN-15: worker Lambda + IAM
│   ├── approval.tf          # SEN-24-27: API GW, executor
│   ├── variables.tf
│   ├── personal.tfvars      # gitignored
│   └── learning.tfvars      # gitignored
├── src/
│   ├── target_app/          # the app that breaks on purpose
│   ├── ingest/              # alarm → incident, dedup
│   ├── agent/
│   │   ├── config.py
│   │   ├── bedrock.py       # client wrapper w/ backoff
│   │   └── tools/
│   └── executor/
├── evals/
│   ├── scenarios/
│   └── harness.py
├── frontend/
├── .github/workflows/ci.yml
└── deploy.ps1