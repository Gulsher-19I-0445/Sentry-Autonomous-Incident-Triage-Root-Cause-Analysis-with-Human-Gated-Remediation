// The demo application — the thing that fails on purpose.
//
// Optional, but on by default: without something that breaks, the pipeline has
// nothing to investigate and a fresh `apply` produces an inert deployment.
// Set create_target_app = false to point Sentry at applications you already
// run, and list them in investigation_targets instead.
//
// This is the SUBJECT of the evaluation, not part of it. Nothing here may
// reveal that its failures are injected: the agent reads these log groups as
// primary evidence, and a log line or symbol name that gives the game away
// invalidates the whole experiment.

data "archive_file" "target_app" {
  count       = var.create_target_app ? 1 : 0
  type        = "zip"
  source_dir  = "${path.module}/../src"
  output_path = "${path.module}/.build/app.zip"

  excludes = [
    "sentry",
    "sentry/**",
    "**/__pycache__",
    "**/__pycache__/**",
    "**/*.pyc",
  ]
}

resource "aws_dynamodb_table" "app" {
  count        = var.create_target_app ? 1 : 0
  name         = local.name["app"]
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  server_side_encryption {
    enabled = true
  }
}

resource "aws_sqs_queue" "orders_dlq" {
  count                     = var.create_target_app ? 1 : 0
  name                      = local.name["orders-dlq"]
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "orders" {
  count                      = var.create_target_app ? 1 : 0
  name                       = local.name["orders"]
  sqs_managed_sse_enabled    = true
  visibility_timeout_seconds = 90

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.orders_dlq[0].arn
    # Higher than the work queue's: the DLQ filling up is itself one of the
    # failure modes under test, so messages need room to retry into it.
    maxReceiveCount = 3
  })
}

// --------------------------------------------------------------------------
// roles
// --------------------------------------------------------------------------

resource "aws_iam_role" "target_api" {
  count              = var.create_target_app ? 1 : 0
  name               = "${local.name["api"]}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "target_api_logs" {
  count      = var.create_target_app ? 1 : 0
  role       = aws_iam_role.target_api[0].name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "target_api" {
  count = var.create_target_app ? 1 : 0

  statement {
    actions   = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:DeleteItem"]
    resources = [aws_dynamodb_table.app[0].arn]
  }

  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.orders[0].arn]
  }
}

resource "aws_iam_role_policy" "target_api" {
  count  = var.create_target_app ? 1 : 0
  name   = "api"
  role   = aws_iam_role.target_api[0].id
  policy = data.aws_iam_policy_document.target_api[0].json
}

resource "aws_iam_role" "target_consumer" {
  count              = var.create_target_app ? 1 : 0
  name               = "${local.name["consumer"]}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "target_consumer_logs" {
  count      = var.create_target_app ? 1 : 0
  role       = aws_iam_role.target_consumer[0].name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "target_consumer" {
  count = var.create_target_app ? 1 : 0

  statement {
    actions   = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.app[0].arn]
  }

  statement {
    actions = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [
      aws_sqs_queue.orders[0].arn,
      aws_sqs_queue.orders_dlq[0].arn,
    ]
  }

  # Deliberately NOT granted s3:GetObject. One evaluation scenario depends on a
  # genuine AccessDenied from a real missing permission — faking the error text
  # would be a different and much weaker test.
}

resource "aws_iam_role_policy" "target_consumer" {
  count  = var.create_target_app ? 1 : 0
  name   = "consumer"
  role   = aws_iam_role.target_consumer[0].id
  policy = data.aws_iam_policy_document.target_consumer[0].json
}

// --------------------------------------------------------------------------
// functions
// --------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "target_api" {
  count             = var.create_target_app ? 1 : 0
  name              = "/aws/lambda/${local.name["api"]}"
  retention_in_days = var.log_retention_days
}

resource "random_password" "admin_token" {
  count   = var.create_target_app ? 1 : 0
  length  = 32
  special = false
}

resource "aws_lambda_function" "target_api" {
  count         = var.create_target_app ? 1 : 0
  function_name = local.name["api"]
  role          = aws_iam_role.target_api[0].arn
  handler       = "target_app.api.handler.handler"
  runtime       = "python3.12"
  timeout       = 30
  memory_size   = 256

  filename         = data.archive_file.target_app[0].output_path
  source_code_hash = data.archive_file.target_app[0].output_base64sha256

  environment {
    variables = {
      TABLE_NAME   = aws_dynamodb_table.app[0].name
      QUEUE_URL    = aws_sqs_queue.orders[0].url
      SERVICE_NAME = "api"
      STAGE        = var.owner
      # Gates the endpoints that arm a failure mode. Generated, not written
      # down, so it cannot be committed by accident.
      ADMIN_TOKEN = random_password.admin_token[0].result
    }
  }

  depends_on = [aws_cloudwatch_log_group.target_api]
}

resource "aws_cloudwatch_log_group" "target_consumer" {
  count             = var.create_target_app ? 1 : 0
  name              = "/aws/lambda/${local.name["consumer"]}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "target_consumer" {
  count         = var.create_target_app ? 1 : 0
  function_name = local.name["consumer"]
  role          = aws_iam_role.target_consumer[0].arn
  handler       = "target_app.consumer.handler.handler"
  runtime       = "python3.12"
  timeout       = 60

  # 256MB is load-bearing, not arbitrary: one scenario exhausts memory, and a
  # larger ceiling makes it hit the invocation timeout first — which is a
  # different failure with different evidence.
  memory_size = 256

  filename         = data.archive_file.target_app[0].output_path
  source_code_hash = data.archive_file.target_app[0].output_base64sha256

  environment {
    variables = {
      TABLE_NAME   = aws_dynamodb_table.app[0].name
      SERVICE_NAME = "consumer"
      STAGE        = var.owner
      # PAYMENT_GATEWAY_URL is intentionally absent — one scenario needs a real
      # KeyError from a genuinely missing variable.
    }
  }

  depends_on = [aws_cloudwatch_log_group.target_consumer]
}

resource "aws_lambda_event_source_mapping" "orders_to_consumer" {
  count            = var.create_target_app ? 1 : 0
  event_source_arn = aws_sqs_queue.orders[0].arn
  function_name    = aws_lambda_function.target_consumer[0].arn
  batch_size       = 1
}

// A published version and a `live` alias, so there is something to roll back
// to. Without a prior version the executor has no valid target and correctly
// refuses to act.
resource "aws_lambda_alias" "target_api_live" {
  count            = var.create_target_app ? 1 : 0
  name             = "live"
  function_name    = aws_lambda_function.target_api[0].function_name
  function_version = aws_lambda_function.target_api[0].version
}

resource "aws_lambda_function_url" "target_api" {
  count              = var.create_target_app ? 1 : 0
  function_name      = aws_lambda_function.target_api[0].function_name
  authorization_type = "NONE"

  # The demo app is deliberately reachable so traffic can be driven against it.
  # It stores nothing sensitive; the admin routes are token-gated.

  # Without this the dashboard cannot drive the app from a browser. CORS is not
  # an authorisation boundary — the admin token still gates the admin routes —
  # it only decides whose JavaScript may read the reply. Naming x-admin-token
  # matters twice over: sending it is what forces a preflight in the first
  # place, so omitting it here fails the request before it is ever attempted.
  #
  # DELETE is here and absent from the approval gate's list because disarming a
  # mode is a DELETE and the gate has no delete route.
  cors {
    allow_origins = ["*"]
    allow_methods = ["GET", "POST", "DELETE"]
    allow_headers = ["content-type", "x-admin-token"]
    max_age       = 3600
  }
}
