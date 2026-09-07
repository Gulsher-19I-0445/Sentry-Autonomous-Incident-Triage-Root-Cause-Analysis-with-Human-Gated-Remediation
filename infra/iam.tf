// The safety argument lives here, not in the prompt.
//
// The agent that reads evidence and the executor that changes things are
// separate functions with separate roles. The agent's role grants no mutating
// action on anything it investigates, so a prompt injection or a reasoning
// failure can produce a wrong *proposal* but cannot produce a wrong *action*.
// Prompt instructions are advice; this file is enforcement.
//
// Every policy is scoped to specific ARNs. Wildcards appear only where the API
// genuinely has no resource-level authorisation — CloudWatch metrics and
// CloudTrail lookups — and those are filtered in application code instead.

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

// --------------------------------------------------------------------------
// ingest — SNS to SQS, no reasoning
// --------------------------------------------------------------------------

resource "aws_iam_role" "ingest" {
  name               = "${local.name["ingest"]}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "ingest_logs" {
  role       = aws_iam_role.ingest.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "ingest" {
  statement {
    sid       = "EnqueueWork"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.work.arn]
  }

  statement {
    sid = "DeduplicateIncidents"
    # No DeleteItem: ingest creates and counts, it never removes.
    actions   = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem"]
    resources = [aws_dynamodb_table.incidents.arn]
  }
}

resource "aws_iam_role_policy" "ingest" {
  name   = "ingest"
  role   = aws_iam_role.ingest.id
  policy = data.aws_iam_policy_document.ingest.json
}

// --------------------------------------------------------------------------
// agent — READ ONLY, and the reason this project has a safety story
// --------------------------------------------------------------------------

resource "aws_iam_role" "agent" {
  name               = "${local.name["agent"]}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json

  description = "Read-only. Grants no mutating action on any investigated resource."
}

resource "aws_iam_role_policy_attachment" "agent_logs" {
  role       = aws_iam_role.agent.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "agent" {
  statement {
    sid     = "InvokeModel"
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = ["arn:${local.partition}:bedrock:*::foundation-model/*",
    "arn:${local.partition}:bedrock:${local.region}:${local.account_id}:inference-profile/*"]
  }

  statement {
    sid = "ReadTargetLogs"
    actions = [
      "logs:StartQuery",
      "logs:GetQueryResults",
      "logs:StopQuery",
      "logs:DescribeLogGroups",
    ]
    # Scoped to the investigated applications only. The agent must not be able
    # to read its OWN log group: doing so would let it consume its previous
    # reasoning as fresh evidence, a feedback loop that is very hard to spot
    # from the outside because the output still looks well-argued.
    resources = local.target_log_group_arns
  }

  statement {
    sid = "ReadMetrics"
    # CloudWatch metrics have no resource-level authorisation, so this cannot
    # be narrowed here. Scoping happens in the tool layer, which resolves short
    # names against an allow-list rather than accepting arbitrary input.
    actions   = ["cloudwatch:GetMetricStatistics", "cloudwatch:GetMetricData", "cloudwatch:ListMetrics"]
    resources = ["*"]
  }

  statement {
    sid = "ReadChangeHistory"
    # Same: LookupEvents is account-wide by design. changes._relevant() drops
    # events for resources outside the target app, including this pipeline's
    # own deploys.
    actions   = ["cloudtrail:LookupEvents"]
    resources = ["*"]
  }

  statement {
    sid = "RecordFindings"
    # The one thing the agent may write, and only to its own incident record.
    # It cannot touch the application's data.
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.incidents.arn]
  }

  dynamic "statement" {
    for_each = local.github_enabled ? [1] : []
    content {
      sid       = "ReadGitHubToken"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = [aws_secretsmanager_secret.github[0].arn]
    }
  }
}

resource "aws_iam_role_policy" "agent" {
  name   = "agent-readonly"
  role   = aws_iam_role.agent.id
  policy = data.aws_iam_policy_document.agent.json
}

// --------------------------------------------------------------------------
// executor — the only role in the system that may change anything
// --------------------------------------------------------------------------

resource "aws_iam_role" "executor" {
  name               = "${local.name["executor"]}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json

  description = "Holds every write permission in the system. Never attach to the agent."
}

resource "aws_iam_role_policy_attachment" "executor_logs" {
  role       = aws_iam_role.executor.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "executor" {
  # Only present when there is a Lambda target to roll back. A deployment
  # investigating non-Lambda workloads gets no mutation permissions at all,
  # and escalates everything — which is correct, not a limitation.
  dynamic "statement" {
    for_each = length(local.target_function_arns) > 0 ? [1] : []
    content {
      sid = "ShiftAliasOnTargets"
      actions = [
        "lambda:GetAlias",
        "lambda:UpdateAlias",
        "lambda:ListVersionsByFunction",
        "lambda:GetFunctionConfiguration",
      ]
      # Enumerated target functions only. Notably absent: UpdateFunctionCode,
      # DeleteFunction, and anything on the pipeline's own functions — the
      # executor cannot modify itself or the agent.
      resources = local.target_function_arns
    }
  }

  # Only granted when a flag table is named. The executor may turn a flag off
  # and never on: off is the recoverable direction, on is a change whose blast
  # radius nobody has reasoned about.
  dynamic "statement" {
    for_each = var.app_flag_table != "" ? [1] : []
    content {
      sid     = "DisableFeatureFlag"
      actions = ["dynamodb:UpdateItem", "dynamodb:Scan"]
      resources = [
        "arn:${local.partition}:dynamodb:${local.region}:${local.account_id}:table/${var.app_flag_table}"
      ]
    }
  }

  statement {
    sid       = "RecordExecution"
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.incidents.arn]
  }
}

resource "aws_iam_role_policy" "executor" {
  name   = "executor-write"
  role   = aws_iam_role.executor.id
  policy = data.aws_iam_policy_document.executor.json
}

// --------------------------------------------------------------------------
// approval — authorises, never acts
// --------------------------------------------------------------------------

resource "aws_iam_role" "approval" {
  name               = "${local.name["approval"]}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json

  description = "May authorise a remediation and invoke the executor. Holds no write permission of its own."
}

resource "aws_iam_role_policy_attachment" "approval_logs" {
  role       = aws_iam_role.approval.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "approval" {
  statement {
    sid       = "ReadAndTransitionIncidents"
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Scan"]
    resources = [aws_dynamodb_table.incidents.arn]
  }

  statement {
    sid = "InvokeExecutor"
    # The separation that makes "who approved" and "what was done" two
    # independent audit facts: this role can start the executor but cannot
    # perform any action the executor performs.
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.executor.arn]
  }
}

resource "aws_iam_role_policy" "approval" {
  name   = "approval"
  role   = aws_iam_role.approval.id
  policy = data.aws_iam_policy_document.approval.json
}
