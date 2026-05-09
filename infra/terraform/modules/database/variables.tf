variable "name_prefix" {
  description = "Resource-name prefix. Drives the cluster identifier, parameter group name, KMS alias, secret path, and SG name."
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
  description = "Master username for the cluster. Stored in the Secrets Manager secret alongside a generated random password."
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
  description = "Idle seconds before scaling to 0 ACU. Minimum 300 (5 minutes) per AWS."
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

variable "secret_recovery_window_days" {
  description = "Recovery window for the master-credentials secret on destroy. 0 = immediate, 7-30 = recovery period."
  type        = number
  default     = 7
}
