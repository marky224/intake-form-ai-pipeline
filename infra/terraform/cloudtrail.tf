# Project audit trail. Captures management events account-wide for
# us-east-1 plus S3 object-level data events on the three sensitive
# buckets (documents, artifacts, tfstate). RDS doesn't have an S3-style
# data-event surface — its mutating actions are management events that
# the management-event capture below already covers.
#
# Single-region (us-east-1 only) matches the rest of the stack; the
# project doesn't currently provision resources in any other region.
# is_organization_trail = false because this is a single-account
# portfolio. Log file validation is on so the digest files allow
# tamper-detection downstream.
#
# CW Logs delivery is intentionally not wired here. The metric-filter +
# SNS alarm path is compute-layer territory (no SOC-style monitoring
# story exists yet to attach to), and CW Logs ingest pricing
# ($0.50/GB) compounds quickly. Revisit when there's something
# concrete to alarm on.
#
# Trail bucket encryption stays on SSE-S3 (matches the project's
# existing locked posture for state and project buckets — same threat
# model: account ID + ARNs + IAM principal names, no customer data).
resource "aws_cloudtrail" "this" {
  # checkov:skip=CKV_AWS_35:SSE-S3 (AES256) on the trail bucket matches the project's locked posture for state and project buckets — same threat model (account ID + ARNs + IAM principals, no customer data); KMS adds cost without changing the model. CLAUDE.md "Build gotchas" carries the long-form reasoning.
  # checkov:skip=CKV_AWS_67:Single-region (us-east-1) by design — the project does not provision resources in other regions, so a multi-region trail would just emit empty events from regions with no project activity. include_global_service_events covers IAM/STS/CloudFront which are global-service events delivered to whichever region's trail is enabled.
  # checkov:skip=CKV_AWS_252:SNS topic + alerting are compute-layer territory, deferred until a SOC-style monitoring story exists to attach to. Same deferral pattern as the CW Logs delivery skip below.
  # checkov:skip=CKV2_AWS_10:S3-only delivery is the locked tradeoff. CW Logs ingest is $0.50/GB and metric-filter/SNS alerting belongs in the compute layer; revisit once there's something concrete to alarm on.
  name           = local.cloudtrail_trail_name
  s3_bucket_name = module.cloudtrail_logs_bucket.bucket_id
  s3_key_prefix  = "cloudtrail"

  include_global_service_events = true
  is_multi_region_trail         = false
  enable_log_file_validation    = true
  enable_logging                = true

  # Management events: read+write across all resource types in
  # us-east-1 for this account. Captures RDS mutations, IAM changes,
  # KMS operations, etc.
  event_selector {
    read_write_type           = "All"
    include_management_events = true

    # S3 object-level data events on the three sensitive buckets.
    # tfstate ARN is reconstructed from project + account because the
    # bucket lives in the bootstrap stack; no terraform_remote_state
    # data source needed for a string concatenation.
    data_resource {
      type = "AWS::S3::Object"
      values = [
        "arn:aws:s3:::${local.documents_bucket_name}/",
        "arn:aws:s3:::${local.artifacts_bucket_name}/",
        "arn:aws:s3:::${local.state_bucket_name}/",
      ]
    }
  }

  depends_on = [module.cloudtrail_logs_bucket]
}
