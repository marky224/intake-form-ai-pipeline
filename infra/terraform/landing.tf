# Landing bucket: CloudFront origin for the wake-on-request demo page.
# Today this serves a placeholder index.html + robots.txt; Phase 7 swaps
# the contents for the actual React review UI bundle. Same hardening as
# documents/artifacts (versioned, AES256, public-access-block, TLS-only
# deny, S3 access logging) plus an OAC-scoped GetObject grant so the
# CloudFront distribution can read objects without making the bucket
# public.
#
# Deliberately separate from documents/artifacts for blast-radius
# reasons: the landing bucket is the only one fronted by a public
# distribution. Mixing the SPA bundle with the documents-bucket would
# either widen what's reachable through CloudFront or require a more
# elaborate origin-level path filter.
module "landing_bucket" {
  source = "./modules/storage"

  bucket_name = local.landing_bucket_name
  purpose     = "landing"

  logging_target_bucket = local.access_logs_bucket_name
  logging_target_prefix = "landing/"

  extra_bucket_policy_statements = [
    {
      sid     = "AllowCloudFrontOACRead"
      effect  = "Allow"
      actions = ["s3:GetObject"]
      principals = [
        {
          type        = "Service"
          identifiers = ["cloudfront.amazonaws.com"]
        }
      ]
      resources = ["arn:aws:s3:::${local.landing_bucket_name}/*"]
      conditions = [
        {
          test     = "StringEquals"
          variable = "aws:SourceArn"
          values   = [aws_cloudfront_distribution.this.arn]
        },
      ]
    },
  ]

  depends_on = [module.access_logs_bucket]
}

# Placeholder objects for PR 5b. Phase 7 replaces index.html with the
# React bundle (multiple objects under /assets/ + a transformed root
# index.html). robots.txt stays Disallow-all for the lifetime of the
# project — this is a demo behind a private hosted zone, not something
# we want indexed.
#
# `etag = filemd5(...)` triggers re-upload on content change, which is
# how the Phase 7 swap will work. `content_type` set explicitly so
# CloudFront can serve the right MIME without origin metadata round-trips.
resource "aws_s3_object" "index_html" {
  bucket       = module.landing_bucket.bucket_id
  key          = "index.html"
  source       = "${path.module}/landing/index.html"
  etag         = filemd5("${path.module}/landing/index.html")
  content_type = "text/html; charset=utf-8"
}

resource "aws_s3_object" "robots_txt" {
  bucket       = module.landing_bucket.bucket_id
  key          = "robots.txt"
  source       = "${path.module}/landing/robots.txt"
  etag         = filemd5("${path.module}/landing/robots.txt")
  content_type = "text/plain; charset=utf-8"
}
