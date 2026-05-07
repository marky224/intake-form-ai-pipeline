output "state_bucket_name" {
  description = "S3 bucket holding Terraform state. Used in .tfbackend for remote-state config."
  value       = aws_s3_bucket.tfstate.id
}

output "state_lock_table_name" {
  description = "DynamoDB table for state locking. Used in .tfbackend for remote-state config."
  value       = aws_dynamodb_table.tflock.id
}

output "ci_role_arn" {
  description = "Role ARN for GitHub Actions to assume via OIDC. Set as the AWS_OIDC_ROLE_ARN repo variable in GitHub."
  value       = aws_iam_role.github_actions.arn
}

output "oidc_provider_arn" {
  description = "ARN of the GitHub OIDC provider in IAM."
  value       = aws_iam_openid_connect_provider.github.arn
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
