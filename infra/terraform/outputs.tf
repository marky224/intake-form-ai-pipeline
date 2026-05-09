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
  description = "ARN of the Secrets Manager secret holding master credentials + endpoint metadata."
  value       = module.database.secret_arn
}

output "aurora_secret_name" {
  description = "Name of the Aurora master-credentials secret."
  value       = module.database.secret_name
}

output "aurora_security_group_id" {
  description = "Security group attached to the Aurora cluster. Reference this when wiring Lambda or bastion ingress."
  value       = module.database.security_group_id
}

output "aurora_kms_key_arn" {
  description = "ARN of the customer-managed KMS key encrypting the cluster volume and master secret."
  value       = module.database.kms_key_arn
}
