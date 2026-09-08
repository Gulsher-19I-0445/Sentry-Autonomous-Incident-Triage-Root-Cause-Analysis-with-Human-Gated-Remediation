terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State is local by default so a clone runs with no prerequisites.
  #
  # State contains every resource attribute in plaintext, including the
  # generated approval token — which is why *.tfstate is gitignored, and why
  # you should not paste plan output into a public issue.
  #
  # For anything shared or long-lived, move it to S3. The bucket has to exist
  # before `init`, so it is bootstrapped by hand rather than by this config:
  #
  #   backend "s3" {
  #     bucket       = "your-tf-state-bucket"
  #     key          = "sentry/terraform.tfstate"
  #     region       = "us-east-1"
  #     encrypt      = true
  #     use_lockfile = true
  #   }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = merge(var.tags, {
      Project   = var.project_name
      Owner     = var.owner
      ManagedBy = "terraform"
    })
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

resource "random_password" "approval_token" {
  length  = 40
  special = false # travels in an HTTP header; keep it URL-safe
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name
  partition  = data.aws_partition.current.partition

  # Matches the existing convention: <project>-<component>-<owner>. Every name
  # in this config goes through here, so a second deployment into the same
  # account collides with nothing as long as `owner` differs.
  name = { for component in [
    "api", "consumer", "ingest", "agent", "executor", "approval",
    "app", "incidents", "orders", "orders-dlq", "work", "work-dlq",
    "alarms", "notify", "github",
  ] : component => "${var.project_name}-${component}-${var.owner}" }

  lambda_prefix = "arn:${local.partition}:lambda:${local.region}:${local.account_id}:function"

  # The applications this deployment may investigate. Sentry does not deploy
  # any of them: what it watches is somebody else's software, including when
  # that somebody is you.
  targets = var.investigation_targets

  target_log_groups = [for t in local.targets : t.log_group]

  # Only Lambda targets can be rolled back. Others are still investigated; the
  # agent escalates instead of acting, which is the designed behaviour when no
  # safe automated remediation exists.
  target_functions = compact([for t in local.targets : try(t.lambda_function_name, null)])

  target_function_arns = flatten([
    for fn in local.target_functions : [
      "${local.lambda_prefix}:${fn}",
      "${local.lambda_prefix}:${fn}:*",
    ]
  ])

  target_log_group_arns = [
    for lg in local.target_log_groups :
    "arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:${lg}:*"
  ]

  github_enabled = var.github_repo != ""
}

# Fail at plan time rather than deploying something inert.
resource "terraform_data" "target_check" {
  lifecycle {
    precondition {
      condition     = length(var.investigation_targets) > 0
      error_message = "Set investigation_targets, or the agent has nothing to investigate."
    }
  }
}
