variable "name_prefix" {
  description = "Resource-name prefix. Drives the cluster identifier, parameter group name, KMS alias, log group name, and SG name."
  type        = string
}

variable "vpc_id" {
  description = "VPC the cluster security group attaches to."
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet IDs for the DB subnet group. Aurora picks AZs from these; min 2 distinct AZs required by AWS even for single-instance Serverless v2."
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "private_subnet_ids must contain at least 2 subnets in distinct AZs."
  }
}

variable "engine_version" {
  description = "Aurora PostgreSQL engine version. Pinned to a specific minor for reproducibility."
  type        = string
  default     = "16.4"
}

variable "master_username" {
  description = "Master username for the cluster. Stored in the AWS-managed Secrets Manager secret alongside an AWS-generated password (manage_master_user_password = true)."
  type        = string
  default     = "intake_admin"
}

variable "database_name" {
  description = "Initial database name created with the cluster. Schemas (demo/eval/staging) are added post-apply via just db-init-schemas."
  type        = string
  default     = "intake"
}

variable "min_capacity" {
  description = "Aurora Serverless v2 minimum ACU. 0 enables auto-pause after seconds_until_auto_pause idle."
  type        = number
  default     = 0
}

variable "max_capacity" {
  description = "Aurora Serverless v2 maximum ACU. 1.0 caps cost; raise as eval workload grows."
  type        = number
  default     = 1.0
}

variable "seconds_until_auto_pause" {
  description = "Idle seconds before scaling to 0 ACU. Minimum 300 (5 minutes) per AWS. Ignored when min_capacity > 0."
  type        = number
  default     = 300

  validation {
    condition     = var.seconds_until_auto_pause >= 300
    error_message = "seconds_until_auto_pause must be >= 300 (AWS minimum)."
  }
}

variable "backup_retention_period" {
  description = "Days to retain automated backups. 1 day matches portfolio-cost posture; raise for production."
  type        = number
  default     = 1
}

variable "log_retention_days" {
  description = "Retention days for the postgresql CloudWatch log group. Aurora Serverless v2 logs aren't massive at portfolio scale, so 30 days is a sensible default for forensic visibility."
  type        = number
  default     = 30
}
