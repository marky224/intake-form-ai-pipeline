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
