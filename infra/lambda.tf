// The four pipeline functions.
//
// One zip serves all four; only the handler string differs. That mirrors how
// the bundles are built by hand and keeps shared code genuinely shared rather
// than duplicated per function.

// archive_file zips the CONTENTS of source_dir, so pointing it at src/sentry
// would put `agent/`, `ingest/` at the zip root and every handler string would
// fail with "No module named 'sentry'". Point it at src/ and exclude the other
// bundle instead, so the zip root holds `sentry/` exactly as the hand-built
// one does.
data "archive_file" "sentry" {
  type        = "zip"
  source_dir  = "${path.module}/../src"
  output_path = "${path.module}/.build/sentry.zip"

  excludes = [
    "target_app",
    "target_app/**",
    # Compiled artefacts differ between machines and would otherwise show as a
    # spurious code change on every apply from a different laptop.
    "**/__pycache__",
    "**/__pycache__/**",
    "**/*.pyc",
  ]
}

locals {
  sentry_source_hash = data.archive_file.sentry.output_base64sha256

  common_env = {
    INCIDENTS_TABLE   = aws_dynamodb_table.incidents.name
    INCIDENT_TTL_DAYS = tostring(var.incident_ttl_days)
    LOG_LEVEL         = "INFO"
  }
}

// --------------------------------------------------------------------------
// ingest
// --------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "ingest" {
  name              = "/aws/lambda/${local.name["ingest"]}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "ingest" {
  function_name = local.name["ingest"]
  role          = aws_iam_role.ingest.arn
  handler       = "sentry.ingest.handler.handler"
  runtime       = "python3.12"
  timeout       = 30
  memory_size   = 256

  filename         = data.archive_file.sentry.output_path
  source_code_hash = local.sentry_source_hash

  environment {
    variables = merge(local.common_env, {
      WORK_QUEUE_URL       = aws_sqs_queue.work.url
      DEDUP_WINDOW_SECONDS = "300"
    })
  }

  depends_on = [aws_cloudwatch_log_group.ingest]
}

// --------------------------------------------------------------------------
// agent
// --------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "agent" {
  name              = "/aws/lambda/${local.name["agent"]}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "agent" {
  function_name = local.name["agent"]
  role          = aws_iam_role.agent.arn
  handler       = "sentry.agent.handler.handler"
  runtime       = "python3.12"
  timeout       = var.agent_timeout_seconds
  memory_size   = var.agent_memory_mb

  filename         = data.archive_file.sentry.output_path
  source_code_hash = local.sentry_source_hash

  environment {
    variables = merge(local.common_env, {
      AGENT_MODEL_ID      = var.agent_model_id
      MAX_AGENT_STEPS     = tostring(var.max_agent_steps)
      LOG_WINDOW_MINUTES  = tostring(var.log_window_minutes)
      ENABLE_PROMPT_CACHE = tostring(var.enable_prompt_cache)

      # The allow-lists the tool layer resolves short names against. This is
      # the second half of the scoping story: IAM stops the agent reading
      # anything else, and these stop it trying.
      TARGET_LOG_GROUPS = join(",", local.target_log_groups)
      TARGET_FUNCTIONS  = join(",", local.target_functions)

      GITHUB_REPO         = var.github_repo
      GITHUB_TOKEN_SECRET = local.github_enabled ? aws_secretsmanager_secret.github[0].name : ""
    })
  }

  depends_on = [aws_cloudwatch_log_group.agent]
}

resource "aws_lambda_event_source_mapping" "work_to_agent" {
  event_source_arn = aws_sqs_queue.work.arn
  function_name    = aws_lambda_function.agent.arn

  # One incident per invocation. Batching would put several investigations
  # behind a single visibility timeout and one failure would redeliver them all.
  batch_size                         = 1
  maximum_batching_window_in_seconds = 0

  # The handler returns batchItemFailures, so a failed incident is retried
  # without redelivering its neighbours.
  function_response_types = ["ReportBatchItemFailures"]
}

// --------------------------------------------------------------------------
// executor
// --------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "executor" {
  name              = "/aws/lambda/${local.name["executor"]}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "executor" {
  function_name = local.name["executor"]
  role          = aws_iam_role.executor.arn
  handler       = "sentry.executor.handler.handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  filename         = data.archive_file.sentry.output_path
  source_code_hash = local.sentry_source_hash

  environment {
    variables = merge(local.common_env, {
      APP_TABLE  = var.app_flag_table
      LIVE_ALIAS = "live"

      # The executor's second guard: _resolve_function() matches the RCA's
      # free-text affected_component against this list and refuses anything
      # else. Without it the shared Config falls back to a hardcoded default,
      # and the guard silently runs on another deployment's function names.
      TARGET_FUNCTIONS = join(",", local.target_functions)
    })
  }

  depends_on = [aws_cloudwatch_log_group.executor]
}

// --------------------------------------------------------------------------
// approval gate
// --------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "approval" {
  name              = "/aws/lambda/${local.name["approval"]}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "approval" {
  function_name = local.name["approval"]
  role          = aws_iam_role.approval.arn
  handler       = "sentry.approval.handler.handler"
  runtime       = "python3.12"
  timeout       = 30
  memory_size   = 256

  filename         = data.archive_file.sentry.output_path
  source_code_hash = local.sentry_source_hash

  environment {
    variables = merge(local.common_env, {
      APPROVAL_TOKEN    = random_password.approval_token.result
      EXECUTOR_FUNCTION = aws_lambda_function.executor.function_name
      ALLOWED_ORIGIN    = "*"
    })
  }

  depends_on = [aws_cloudwatch_log_group.approval]
}

resource "aws_lambda_function_url" "approval" {
  function_name = aws_lambda_function.approval.function_name

  # The shared token in the x-approval-token header is the gate, checked in the
  # handler, which fails closed when the token is unset.
  #
  # This is honest-but-modest security: it stops an accidental request, not a
  # determined attacker. A production deployment would put an authorizer or
  # IAM auth in front. Say so in the write-up rather than implying otherwise.
  authorization_type = "NONE"

  cors {
    allow_origins = ["*"]
    allow_methods = ["GET", "POST"]
    allow_headers = ["content-type", "x-approval-token", "x-actor"]
    max_age       = 3600
  }
}

// Creating the URL does not make it reachable. The console adds this statement
// for you; CreateFunctionUrlConfig, which is what Terraform calls, does not —
// so without it every request is refused with 403 before the handler runs and
// the token check never happens. auth_type NONE means "no IAM", not "no
// resource policy".
resource "aws_lambda_permission" "approval_url" {
  statement_id           = "AllowFunctionURLInvoke"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.approval.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}
