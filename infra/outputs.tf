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

output "target_api_url" {
  description = "Demo application endpoint, when the bundled app is deployed."
  value       = var.create_target_app ? aws_lambda_function_url.target_api[0].function_url : null
}

output "target_admin_token" {
  description = "Arms a failure mode on the demo app. terraform output -raw target_admin_token"
  value       = var.create_target_app ? random_password.admin_token[0].result : null
  sensitive   = true
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
       then fill in both URLs, which are not secret:
         terraform output -raw approval_url
         terraform output -raw target_api_url

       Open frontend/index.html and paste the two tokens, which are:
         terraform output -raw approval_token
         terraform output -raw target_admin_token
       Deploy config.json alongside the page and anyone you share it with only
       has to paste those two. Use "How to run" in the page to drive it.

    ${local.github_enabled ? "2. Populate the GitHub secret (Terraform never sees it):\n         aws secretsmanager put-secret-value --secret-id ${local.name["github"]} --secret-string '{\"token\":\"github_pat_...\"}'\n" : "2. GitHub is disabled. Set github_repo to let the agent read commits as evidence.\n"}
    3. Bedrock model access must be granted for ${var.agent_model_id}
       in ${local.region}. It is not requestable through Terraform.

    ${var.create_target_app ? "4. Drive traffic at target_api_url to produce something to investigate." : "4. Alarms exist only for Lambda targets; define your own for anything else."}

  EOT
}
