output "cluster_endpoint" {
  description = "Writer endpoint for the Aurora cluster."
  value       = aws_rds_cluster.this.endpoint
}

output "cluster_reader_endpoint" {
  description = "Reader endpoint for the Aurora cluster (same writer host on a single-instance cluster, kept for forward-compat with read replicas)."
  value       = aws_rds_cluster.this.reader_endpoint
}

output "cluster_port" {
  description = "Port the cluster listens on (5432 for PostgreSQL)."
  value       = aws_rds_cluster.this.port
}

output "cluster_identifier" {
  description = "Cluster identifier, used as the prefix for cluster-scoped resources."
  value       = aws_rds_cluster.this.cluster_identifier
}

output "secret_arn" {
  description = "ARN of the Secrets Manager secret holding master credentials + endpoint metadata."
  value       = aws_secretsmanager_secret.master.arn
}

output "secret_name" {
  description = "Name of the Secrets Manager secret. Path-prefixed under the project namespace."
  value       = aws_secretsmanager_secret.master.name
}

output "kms_key_arn" {
  description = "ARN of the customer-managed KMS key encrypting the cluster volume and the master secret."
  value       = aws_kms_key.aurora.arn
}

output "kms_key_alias" {
  description = "Alias of the customer-managed KMS key."
  value       = aws_kms_alias.aurora.name
}

output "security_group_id" {
  description = "Security group attached to the cluster. Add Lambda/bastion ingress rules referencing this SG."
  value       = aws_security_group.cluster.id
}
