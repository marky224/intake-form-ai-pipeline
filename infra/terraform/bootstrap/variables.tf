variable "aws_region" {
  description = "AWS region for the state bucket, lock table, and OIDC role."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier used for resource naming and the Project tag."
  type        = string
  default     = "intake-form-ai-pipeline"
}

variable "github_repo" {
  description = "GitHub repository in <owner>/<name> form. Drives the OIDC trust policy."
  type        = string
  default     = "marky224/intake-form-ai-pipeline"

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repo))
    error_message = "github_repo must be in <owner>/<name> form."
  }
}

variable "github_main_branch" {
  description = "Branch ref allowed to assume the CI role for push-to-main events."
  type        = string
  default     = "main"

  validation {
    condition     = can(regex("^[A-Za-z0-9._/-]+$", var.github_main_branch))
    error_message = "github_main_branch must be a literal branch name (no wildcards or shell metacharacters); patterns would widen the IAM StringLike sub match."
  }
}
