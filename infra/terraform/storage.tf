# Access-logs bucket: receives S3 server access logs from documents +
# artifacts (and any future user-data buckets). The bucket policy grants
# the S3 LogDelivery service principal PutObject scoped via
# aws:SourceAccount + aws:SourceArn (the modern policy-based pattern that
# replaced the legacy LogDelivery ACL grant). Logs expire after 365 days
# to bound storage cost; access-log volume is small at portfolio scale
# but unbounded retention is still an unnecessary tail risk.
module "access_logs_bucket" {
  source = "./modules/storage"

  bucket_name                = local.access_logs_bucket_name
  purpose                    = "access-logs"
  log_object_expiration_days = 365

  # Self-logging is unsupported by S3 (the source bucket and target
  # bucket can't be the same), and a recursive log-of-logs bucket would
  # only shift the problem. Leaving logging disabled here is the
  # documented default; the CKV_AWS_18 skip in the storage module's
  # main.tf carries the rationale.
  logging_target_bucket = null

  extra_bucket_policy_statements = [
    {
      sid     = "AllowS3LogDeliveryWrite"
      effect  = "Allow"
      actions = ["s3:PutObject"]
      principals = [
        {
          type        = "Service"
          identifiers = ["logging.s3.amazonaws.com"]
        }
      ]
      resources = ["arn:aws:s3:::${local.access_logs_bucket_name}/*"]
      conditions = [
        {
          test     = "StringEquals"
          variable = "aws:SourceAccount"
          values   = [local.account_id]
        },
        {
          test     = "ArnLike"
          variable = "aws:SourceArn"
          values = [
            "arn:aws:s3:::${local.documents_bucket_name}",
            "arn:aws:s3:::${local.artifacts_bucket_name}",
          ]
        },
      ]
    },
  ]
}

# CloudTrail logs bucket: receives the project trail's events. The
# bucket policy grants the CloudTrail service principal the canonical
# GetBucketAcl + PutObject pair, scoped via aws:SourceArn to this
# project's trail only. Logs expire after 365 days (mirrors the
# access-logs lifecycle).
module "cloudtrail_logs_bucket" {
  source = "./modules/storage"

  bucket_name                = local.cloudtrail_logs_bucket_name
  purpose                    = "cloudtrail-logs"
  log_object_expiration_days = 365

  # CloudTrail-logs bucket can't usefully receive its own access logs
  # either; same recursion concern as the access-logs bucket above.
  logging_target_bucket = null

  extra_bucket_policy_statements = [
    {
      sid     = "AWSCloudTrailAclCheck"
      effect  = "Allow"
      actions = ["s3:GetBucketAcl"]
      principals = [
        {
          type        = "Service"
          identifiers = ["cloudtrail.amazonaws.com"]
        }
      ]
      resources = ["arn:aws:s3:::${local.cloudtrail_logs_bucket_name}"]
      conditions = [
        {
          test     = "StringEquals"
          variable = "aws:SourceArn"
          values   = [local.cloudtrail_trail_arn]
        },
      ]
    },
    {
      sid     = "AWSCloudTrailWrite"
      effect  = "Allow"
      actions = ["s3:PutObject"]
      principals = [
        {
          type        = "Service"
          identifiers = ["cloudtrail.amazonaws.com"]
        }
      ]
      resources = ["arn:aws:s3:::${local.cloudtrail_logs_bucket_name}/AWSLogs/${local.account_id}/*"]
      conditions = [
        {
          test     = "StringEquals"
          variable = "s3:x-amz-acl"
          values   = ["bucket-owner-full-control"]
        },
        {
          test     = "StringEquals"
          variable = "aws:SourceArn"
          values   = [local.cloudtrail_trail_arn]
        },
      ]
    },
  ]
}

module "documents_bucket" {
  source = "./modules/storage"

  bucket_name = local.documents_bucket_name
  purpose     = "documents"

  # Static bucket-name string (not module.access_logs_bucket.bucket_id)
  # so the count expression on aws_s3_bucket_logging is known at plan
  # time. depends_on enforces creation order.
  logging_target_bucket = local.access_logs_bucket_name
  logging_target_prefix = "documents/"

  depends_on = [module.access_logs_bucket]
}

module "artifacts_bucket" {
  source = "./modules/storage"

  bucket_name = local.artifacts_bucket_name
  purpose     = "artifacts"

  logging_target_bucket = local.access_logs_bucket_name
  logging_target_prefix = "artifacts/"

  depends_on = [module.access_logs_bucket]
}
