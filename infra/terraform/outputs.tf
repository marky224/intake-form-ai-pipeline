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
