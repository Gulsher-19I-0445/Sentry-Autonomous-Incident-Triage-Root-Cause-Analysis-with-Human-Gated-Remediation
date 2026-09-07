// Nothing here prints an account id, and every credential is marked sensitive
// so it is redacted from `apply` output and from CI logs. Read them
// deliberately with `terraform output -raw <name>`.

output "approval_url" {
  description = "Paste into the dashboard along with the token."
  value       = aws_lambda_function_url.approval.function_url
}

output "approval_token" {
  description = "Shared secret for the approval gate. Retrieve with: terraform output -raw approval_token"
  value       = random_password.approval_token.result
  sensitive   = true
}

output "incidents_table" {
  value = aws_dynamodb_table.incidents.name
}

output "agent_function" {
  value = aws_lambda_function.agent.function_name
}

output "agent_log_group" {
  description = "Where investigations are traced. Not readable by the agent itself, by design."
  value       = aws_cloudwatch_log_group.agent.name
}

output "investigation_targets" {
  description = "What this deployment may investigate."
  value = {
    log_groups = local.target_log_groups
    # Targets absent from this list are investigated but never acted on
    # automatically — the agent escalates instead.
    rollback_capable = local.target_functions
  }
}

output "github_secret_name" {
  description = "Populate this out of band; Terraform never holds the token. Null when GitHub is disabled."
  value       = local.github_enabled ? aws_secretsmanager_secret.github[0].name : null
}

output "next_steps" {
  description = "What to do once apply finishes."
  value       = <<-EOT

    1. Point the dashboard at this deployment:
         cp frontend/config.example.json frontend/config.json
       Fill in approval_url, which is not secret:
         terraform output -raw approval_url
       Then open frontend/index.html and paste the token:
         terraform output -raw approval_token

       The application being watched is deployed separately, so its URL and
       admin token are not Terraform outputs. Add them to config.json and the
       page to drive it from "How to run"; leave them out and the dashboard
       still works, without the run controls.

    ${local.github_enabled ? "2. Populate the GitHub secret (Terraform never sees it):\n         aws secretsmanager put-secret-value --secret-id ${local.name["github"]} --secret-string '{\"token\":\"github_pat_...\"}'\n" : "2. GitHub is disabled. Set github_repo to let the agent read commits as evidence.\n"}
    3. Bedrock model access must be granted for ${var.agent_model_id}
       in ${local.region}. It is not requestable through Terraform.

    4. Alarms are created for Lambda targets. For anything else, define your
       own and point them at the alarms topic.

  EOT
}
