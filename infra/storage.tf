// Tables, queues and the alarm topic.

// --------------------------------------------------------------------------
// incidents — the state machine
// --------------------------------------------------------------------------

resource "aws_dynamodb_table" "incidents" {
  name         = local.name["incidents"]
  billing_mode = "PAY_PER_REQUEST" # bursty and near-idle; provisioned would be waste
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}

// --------------------------------------------------------------------------
// work queue — ingest to agent
// --------------------------------------------------------------------------

resource "aws_sqs_queue" "work_dlq" {
  name                      = local.name["work-dlq"]
  message_retention_seconds = 1209600 # 14 days, the maximum
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "work" {
  name                    = local.name["work"]
  sqs_managed_sse_enabled = true

  # Must exceed the agent's own timeout, or SQS redelivers a message that is
  # still being worked and a second investigation starts on the same incident.
  visibility_timeout_seconds = var.agent_timeout_seconds + 30

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.work_dlq.arn
    # Investigations are not idempotent and cost real money, so a poison
    # message gets few attempts. The status guard means a crashed run is never
    # retried anyway; the DLQ is where an operator finds out.
    maxReceiveCount = 2
  })
}

// --------------------------------------------------------------------------
// alarm topic — CloudWatch to ingest
// --------------------------------------------------------------------------

resource "aws_sns_topic" "alarms" {
  name = local.name["alarms"]
}

data "aws_iam_policy_document" "alarms_topic" {
  statement {
    sid     = "AllowCloudWatchAlarms"
    actions = ["SNS:Publish"]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }
    resources = [aws_sns_topic.alarms.arn]

    # Without this, any account's alarms could publish here.
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceOwner"
      values   = [local.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "alarms" {
  arn    = aws_sns_topic.alarms.arn
  policy = data.aws_iam_policy_document.alarms_topic.json
}

resource "aws_sns_topic_subscription" "ingest" {
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.ingest.arn
}

resource "aws_lambda_permission" "sns_invoke_ingest" {
  statement_id  = "AllowSNSInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingest.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.alarms.arn
}

// Optional human notification. Subscription requires confirming an email, so
// it stays "pending confirmation" until someone clicks the link.
resource "aws_sns_topic_subscription" "email" {
  count     = var.alarm_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

// --------------------------------------------------------------------------
// github token — created empty, populated out of band
// --------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "github" {
  count = local.github_enabled ? 1 : 0
  name  = local.name["github"]

  description = "Fine-grained PAT, contents:read on one repository. Value is set outside Terraform."

  # A destroyed secret is otherwise recoverable for 30 days, which blocks
  # recreating it under the same name during iteration.
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "github_placeholder" {
  count     = local.github_enabled ? 1 : 0
  secret_id = aws_secretsmanager_secret.github[0].id

  # A placeholder, never a real token. Supplying the token through Terraform
  # would put it in the plan output, the state file, and any CI log that echoes
  # either. Populate it with:
  #
  #   aws secretsmanager put-secret-value --secret-id <name> \
  #     --secret-string '{"token":"github_pat_..."}'
  #
  # The agent degrades gracefully while it is unset: the commits path logs
  # "github not configured" and returns nothing rather than failing.
  secret_string = jsonencode({ token = "" })

  lifecycle {
    # Terraform will not overwrite the real value on a later apply, and will
    # not show it in a diff.
    ignore_changes = [secret_string]
  }
}
