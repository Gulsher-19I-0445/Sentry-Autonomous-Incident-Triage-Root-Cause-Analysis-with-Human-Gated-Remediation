// The pipeline's trigger.
//
// An alarm only publishes on a state TRANSITION, so one that is already in
// ALARM stays silent no matter how many new errors arrive. That is the single
// most common reason an end-to-end test appears to do nothing.
//
// Alarms are created per Lambda target. Non-Lambda targets are investigated
// too, but their alarms are yours to define — the metric names and dimensions
// depend on the service.

locals {
  lambda_targets = { for t in local.targets : t.name => t
  if try(t.lambda_function_name, null) != null }
}

resource "aws_cloudwatch_metric_alarm" "errors" {
  for_each = local.lambda_targets

  alarm_name        = "${var.project_name}-${each.key}-errors-${var.owner}"
  alarm_description = "Unhandled errors in ${each.value.lambda_function_name}."

  namespace   = "AWS/Lambda"
  metric_name = "Errors"
  dimensions  = { FunctionName = each.value.lambda_function_name }

  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"

  # Without this, a period with no invocations at all is treated as missing
  # data and can hold the alarm in ALARM indefinitely — which then suppresses
  # the next real transition.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "latency" {
  for_each = local.lambda_targets

  alarm_name        = "${var.project_name}-${each.key}-latency-${var.owner}"
  alarm_description = "p95 duration breach in ${each.value.lambda_function_name}."

  namespace          = "AWS/Lambda"
  metric_name        = "Duration"
  dimensions         = { FunctionName = each.value.lambda_function_name }
  extended_statistic = "p95"

  period              = 60
  evaluation_periods  = 1
  threshold           = 3000 # ms
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

// Queue depth, only for the bundled demo app — an externally supplied target
// list carries no queue for Sentry to reason about.
resource "aws_cloudwatch_metric_alarm" "dlq_depth" {
  count = var.create_target_app ? 1 : 0

  alarm_name        = "${var.project_name}-dlq-depth-${var.owner}"
  alarm_description = "Messages are accumulating in the dead letter queue."

  namespace   = "AWS/SQS"
  metric_name = "ApproximateNumberOfMessagesVisible"
  dimensions  = { QueueName = aws_sqs_queue.orders_dlq[0].name }

  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

// The pipeline watching itself. Not wired to the alarm topic on purpose: an
// agent failure must never create an incident for the agent to investigate.
resource "aws_cloudwatch_metric_alarm" "agent_failures" {
  alarm_name        = "${local.name["agent"]}-failures"
  alarm_description = "The investigating agent is failing. Notifies a human directly."

  namespace   = "AWS/Lambda"
  metric_name = "Errors"
  dimensions  = { FunctionName = aws_lambda_function.agent.function_name }

  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 2
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  # Deliberately empty unless an email is configured. Routing this to the
  # alarm topic would make the agent investigate its own crashes — a loop that
  # is expensive, self-reinforcing, and hard to spot from the outside.
  alarm_actions = var.alarm_email != "" ? [aws_sns_topic.alarms.arn] : []
}
