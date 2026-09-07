// Every value that could identify an account, a person, or a region is an
// input with no account-specific default. Nothing here should need editing to
// run in a different account — that is the test for whether this is safe to
// publish.

variable "aws_region" {
  description = "Region for every resource. Bedrock model availability varies by region."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "First segment of every resource name."
  type        = string
  default     = "sentry"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.project_name))
    error_message = "Lowercase letters, digits and hyphens; 2-21 characters."
  }
}

variable "owner" {
  description = <<-EOT
    Last segment of every resource name, so several people can deploy into one
    shared account without colliding. Deliberately has no default: picking one
    would bake whoever wrote this into everyone else's infrastructure.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,20}$", var.owner))
    error_message = "Lowercase letters, digits and hyphens; 2-21 characters."
  }
}

variable "tags" {
  description = "Applied to every taggable resource."
  type        = map(string)
  default     = {}
}

// --------------------------------------------------------------------------
// what the agent is allowed to investigate
// --------------------------------------------------------------------------

variable "investigation_targets" {
  description = <<-EOT
    Applications the agent may investigate. Required — Sentry deploys none of
    them, so without this it has nothing to watch.

    Anything that writes to CloudWatch Logs and emits CloudWatch metrics works:
    Lambda, ECS, EKS via Container Insights, EC2 with the agent. `name` is the
    short label the agent uses to refer to the target.

    `lambda_function_name` is optional and only meaningful for Lambda. It is
    what makes automated rollback possible; targets without it can still be
    investigated, but a remediation will be escalated to a human rather than
    applied, which is the correct behaviour when the system cannot act safely.
  EOT
  type = list(object({
    name                 = string
    log_group            = string
    lambda_function_name = optional(string)
  }))
  default = []

  validation {
    condition     = length(var.investigation_targets) <= 10
    error_message = "Keep the target list small; every target widens what the agent may read."
  }
}

// --------------------------------------------------------------------------
// the model
// --------------------------------------------------------------------------

variable "agent_model_id" {
  description = <<-EOT
    Bedrock inference profile for the investigating agent. The `us.` prefix is
    required — bare model ids are rejected for current Claude models.
  EOT
  type        = string
  default     = "us.anthropic.claude-sonnet-5"
}

variable "max_agent_steps" {
  description = "Hard ceiling on tool-use turns per investigation. Bounds worst-case cost."
  type        = number
  default     = 8
}

variable "log_window_minutes" {
  description = <<-EOT
    How far either side of the incident the agent may query logs and metrics.
    Narrow windows reduce cross-incident contamination; wide ones risk pulling
    in unrelated failures as evidence.
  EOT
  type        = number
  default     = 5
}

variable "enable_prompt_cache" {
  description = <<-EOT
    Send Bedrock cache points so the resent transcript prefix bills at read
    rates. Off by default: the request shape is validated server-side, so a
    rejected one fails the whole investigation rather than degrading.
  EOT
  type        = bool
  default     = false
}

// --------------------------------------------------------------------------
// optional integrations
// --------------------------------------------------------------------------

variable "github_repo" {
  description = <<-EOT
    owner/repo whose commits the agent may read as evidence, or "" to disable.

    The token itself is NEVER supplied through Terraform. This creates an empty
    secret; populate it out of band so the value never enters a plan, a state
    file, or a CI log. See the README.
  EOT
  type        = string
  default     = ""
}

variable "app_flag_table" {
  description = <<-EOT
    DynamoDB table holding feature flags the executor may DISABLE, or "" to
    withhold that remediation entirely.

    Turning a flag off is the recoverable direction, so it is the only write
    granted here; the executor can never turn one on. Naming no table means the
    permission is not granted at all rather than granted and unused — with the
    remediation unavailable, the agent escalates to a human instead, which is
    the correct behaviour when the system cannot act safely.
  EOT
  type        = string
  default     = ""
}

variable "dlq_queue_name" {
  description = <<-EOT
    Queue whose depth should raise an incident, or "" for none.

    Separate from investigation_targets because that list carries log groups
    and functions, not queues. A filling dead-letter queue is often the only
    signal that a consumer is failing silently.
  EOT
  type        = string
  default     = ""
}

variable "alarm_email" {
  description = "Optional address to notify when an incident needs a decision. Empty disables it."
  type        = string
  default     = ""
}

// --------------------------------------------------------------------------
// sizing
// --------------------------------------------------------------------------

variable "agent_timeout_seconds" {
  description = "Investigations are slow; observed mean is around 75 seconds."
  type        = number
  default     = 300
}

variable "agent_memory_mb" {
  description = "The agent is IO-bound on Bedrock, not memory-bound."
  type        = number
  default     = 512
}

variable "log_retention_days" {
  description = "CloudWatch retention. Left unbounded, log storage outlives the project."
  type        = number
  default     = 14
}

variable "incident_ttl_days" {
  description = "Incidents self-delete after this long via a DynamoDB TTL."
  type        = number
  default     = 30
}
