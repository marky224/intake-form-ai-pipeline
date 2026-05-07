output "state_bucket_name" {
  description = "S3 bucket holding Terraform state. Used in .tfbackend for remote-state config."
  value       = aws_s3_bucket.tfstate.id
}

output "state_lock_table_name" {
  description = "DynamoDB table for state locking. Used in .tfbackend for remote-state config."
  value       = aws_dynamodb_table.tflock.id
}

output "deploy_role_arn" {
  description = "Role ARN assumed by GitHub Actions on push to main (write perms). Set as the AWS_OIDC_ROLE_ARN_DEPLOY repo variable."
  value       = aws_iam_role.github_actions_deploy.arn
}

output "plan_role_arn" {
  description = "Role ARN assumed by GitHub Actions on pull_request workflows (ReadOnlyAccess). Set as the AWS_OIDC_ROLE_ARN_PLAN repo variable."
  value       = aws_iam_role.github_actions_plan.arn
}

output "oidc_provider_arn" {
  description = "ARN of the GitHub OIDC provider in IAM (data source — owned out-of-band, shared across projects in this account)."
  value       = data.aws_iam_openid_connect_provider.github.arn
}

output "aws_region" {
  description = "AWS region the bootstrap stack deployed into."
  value       = var.aws_region
}

output "tfbackend_example" {
  description = "Values to copy into .tfbackend after first apply (used with terraform init -migrate-state)."
  value       = <<-EOT
    bucket         = "${aws_s3_bucket.tfstate.id}"
    key            = "bootstrap/terraform.tfstate"
    region         = "${var.aws_region}"
    dynamodb_table = "${aws_dynamodb_table.tflock.id}"
    encrypt        = true
  EOT
}
