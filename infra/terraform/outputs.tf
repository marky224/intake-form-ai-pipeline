output "vpc_id" {
  description = "ID of the project VPC."
  value       = module.network.vpc_id
}

output "public_subnet_ids" {
  description = "IDs of the 3 public subnets."
  value       = module.network.public_subnet_ids
}

output "private_subnet_ids" {
  description = "IDs of the 3 private subnets."
  value       = module.network.private_subnet_ids
}

output "documents_bucket_id" {
  description = "Name of the documents bucket (Synthea + DocILE inputs/outputs)."
  value       = module.documents_bucket.bucket_id
}

output "documents_bucket_arn" {
  description = "ARN of the documents bucket."
  value       = module.documents_bucket.bucket_arn
}

output "artifacts_bucket_id" {
  description = "Name of the artifacts bucket (rendered PDFs, eval fixtures)."
  value       = module.artifacts_bucket.bucket_id
}

output "artifacts_bucket_arn" {
  description = "ARN of the artifacts bucket."
  value       = module.artifacts_bucket.bucket_arn
}

output "aurora_cluster_endpoint" {
  description = "Writer endpoint for the Aurora cluster."
  value       = module.database.cluster_endpoint
}

output "aurora_cluster_port" {
  description = "Port the Aurora cluster listens on."
  value       = module.database.cluster_port
}

output "aurora_secret_arn" {
  description = "ARN of the AWS-managed Secrets Manager secret holding master credentials. Name format: rds!cluster-<UUID>-<6-char-suffix> (RDS owns the namespace; not user-customizable)."
  value       = module.database.secret_arn
}

output "aurora_security_group_id" {
  description = "Security group attached to the Aurora cluster. Reference this when wiring Lambda or bastion ingress."
  value       = module.database.security_group_id
}

output "aurora_kms_key_arn" {
  description = "ARN of the customer-managed KMS key encrypting the cluster volume, master secret, and postgresql log group."
  value       = module.database.kms_key_arn
}

output "aurora_log_group_name" {
  description = "CloudWatch log group receiving the cluster's postgresql audit/error log export."
  value       = module.database.log_group_name
}

output "landing_bucket_id" {
  description = "Name of the landing bucket (CloudFront origin for the demo page)."
  value       = module.landing_bucket.bucket_id
}

output "cloudfront_distribution_domain_name" {
  description = "CloudFront-assigned domain name for the demo distribution. Public DNS resolves the demo_domain through the Route 53 alias to this name."
  value       = aws_cloudfront_distribution.this.domain_name
}

output "cloudfront_distribution_arn" {
  description = "ARN of the demo CloudFront distribution."
  value       = aws_cloudfront_distribution.this.arn
}

output "edge_waf_web_acl_arn" {
  description = "ARN of the CloudFront-fronting WAFv2 web ACL."
  value       = aws_wafv2_web_acl.edge.arn
}

output "demo_url" {
  description = "Public URL for the demo. Resolves to the CloudFront distribution via Route 53 alias once Phase 7's React UI lands; today serves the placeholder index.html."
  value       = "https://${var.demo_domain}/"
}

output "cost_alerts_sns_topic_arn" {
  description = "ARN of the SNS topic receiving budget threshold alerts and Cost Anomaly Detection notifications."
  value       = aws_sns_topic.cost_alerts.arn
}

output "daily_budget_name" {
  description = "Name of the daily-spend budget. Tag-filtered to project-tagged spend; threshold $5/day with a single 100% actual notification (DAILY budgets don't support FORECASTED per the AWS API)."
  value       = aws_budgets_budget.daily_spend.name
}
