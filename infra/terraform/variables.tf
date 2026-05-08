variable "aws_region" {
  description = "AWS region for the main stack. Must match the bootstrap region."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier used for resource naming and the Project tag."
  type        = string
  default     = "intake-form-ai-pipeline"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC. See modules/network/variables.tf for the per-subnet allocation plan."
  type        = string
  default     = "10.0.0.0/16"
}
