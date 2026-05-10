variable "bucket_name" {
  description = "Globally-unique S3 bucket name."
  type        = string
}

variable "purpose" {
  description = "Free-form purpose tag for human identification (e.g., \"documents\", \"artifacts\")."
  type        = string
}

variable "noncurrent_version_expiration_days" {
  description = "Days to retain noncurrent object versions before lifecycle expiry. Mirrors the state bucket default."
  type        = number
  default     = 90

  validation {
    condition     = var.noncurrent_version_expiration_days >= 1
    error_message = "noncurrent_version_expiration_days must be >= 1."
  }
}

variable "force_destroy" {
  description = "Allow Terraform to destroy the bucket even if it contains objects. Default false — protects against accidental data loss."
  type        = bool
  default     = false
}

variable "logging_target_bucket" {
  description = "Bucket id to receive S3 server access logs for this bucket. null disables logging (used for log-target buckets that cannot recursively log to themselves)."
  type        = string
  default     = null
}

variable "logging_target_prefix" {
  description = "Prefix under the logging_target_bucket to write access log objects. Ignored when logging_target_bucket is null."
  type        = string
  default     = null
}

variable "log_object_expiration_days" {
  description = "Days after which current-version log objects expire via lifecycle. null leaves only the existing noncurrent-version expiration in effect (default for documents/artifacts buckets). Set on log-target buckets so logs don't accumulate indefinitely."
  type        = number
  default     = null

  validation {
    condition     = var.log_object_expiration_days == null || var.log_object_expiration_days >= 1
    error_message = "log_object_expiration_days must be null or >= 1."
  }
}

variable "extra_bucket_policy_statements" {
  description = "Additional IAM policy statements composed into the bucket policy alongside the TLS-only deny. Used to grant the S3 LogDelivery group or CloudTrail service principal write access to log-target buckets. Each statement uses the same shape as a `data.aws_iam_policy_document` statement block."
  type = list(object({
    sid     = string
    effect  = string
    actions = list(string)
    principals = optional(list(object({
      type        = string
      identifiers = list(string)
    })), [])
    resources = list(string)
    conditions = optional(list(object({
      test     = string
      variable = string
      values   = list(string)
    })), [])
  }))
  default = []
}
