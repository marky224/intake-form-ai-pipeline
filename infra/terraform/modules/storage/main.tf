resource "aws_s3_bucket" "this" {
  # checkov:skip=CKV_AWS_18:Logging is conditionally wired via aws_s3_bucket_logging when var.logging_target_bucket is set. Buckets passed null (the access-logs and cloudtrail-logs buckets themselves) skip logging by design — recursive self-logging is not supported, and a third-tier "log of logs" bucket would just shift the problem.
  bucket        = var.bucket_name
  force_destroy = var.force_destroy

  tags = {
    Name    = var.bucket_name
    Purpose = var.purpose
  }
}

resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket = aws_s3_bucket.this.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_logging" "this" {
  count = var.logging_target_bucket == null ? 0 : 1

  bucket        = aws_s3_bucket.this.id
  target_bucket = var.logging_target_bucket
  target_prefix = var.logging_target_prefix == null ? "${var.bucket_name}/" : var.logging_target_prefix
}

data "aws_iam_policy_document" "bucket_policy" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.this.arn,
      "${aws_s3_bucket.this.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  dynamic "statement" {
    for_each = var.extra_bucket_policy_statements
    content {
      sid     = statement.value.sid
      effect  = statement.value.effect
      actions = statement.value.actions

      dynamic "principals" {
        for_each = statement.value.principals
        content {
          type        = principals.value.type
          identifiers = principals.value.identifiers
        }
      }

      resources = statement.value.resources

      dynamic "condition" {
        for_each = statement.value.conditions
        content {
          test     = condition.value.test
          variable = condition.value.variable
          values   = condition.value.values
        }
      }
    }
  }
}

resource "aws_s3_bucket_policy" "bucket_policy" {
  bucket = aws_s3_bucket.this.id
  policy = data.aws_iam_policy_document.bucket_policy.json
}

# Renamed from `tls_only` when the policy gained the ability to compose
# additional statements (LogDelivery for access-logs target, CloudTrail
# service principal for cloudtrail-logs target). The data source has no
# AWS-side identity, so only the bucket-policy resource needs migration.
moved {
  from = aws_s3_bucket_policy.tls_only
  to   = aws_s3_bucket_policy.bucket_policy
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_expiration_days
    }
  }

  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  dynamic "rule" {
    for_each = var.log_object_expiration_days == null ? [] : [var.log_object_expiration_days]
    content {
      id     = "expire-log-objects"
      status = "Enabled"

      filter {}

      expiration {
        days = rule.value
      }
    }
  }

  depends_on = [aws_s3_bucket_versioning.this]
}
