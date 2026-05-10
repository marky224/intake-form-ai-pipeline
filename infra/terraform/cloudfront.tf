# CloudFront edge for the demo. Three concerns in this file:
#   1. OAC for S3 origin access (replaces deprecated OAI).
#   2. The distribution itself: WAF-associated, ACM-cert-attached,
#      access-logs-delivered, response-headers-policy-protected.
#   3. v2 access-logs delivery wiring: delivery source on the
#      distribution + delivery destination on the access-logs S3 bucket
#      under cloudfront/ + the delivery linking them.
#
# v2 access logs (CloudWatch Logs Delivery primitives) replace the
# legacy bucket-ACL flow because the access-logs bucket has
# BucketOwnerEnforced on, which disallows ACL grants. The PutObject
# permission for the delivery service principal lives in
# infra/terraform/storage.tf as an extra bucket policy statement on
# the access-logs bucket.

# OAC: signs CloudFront-origin requests with SigV4 so the landing bucket
# can grant `cloudfront.amazonaws.com` GetObject scoped via
# aws:SourceArn to this exact distribution. Replaces OAI (which AWS is
# deprecating). Signing behavior `always` is the default and the only
# value worth using for a static-content origin.
resource "aws_cloudfront_origin_access_control" "landing" {
  name                              = "${var.project_name}-landing-oac"
  description                       = "OAC for the demo landing bucket origin."
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# The distribution. Locked spec:
#   - PriceClass_100 (NA + EU only, lowest cost)
#   - HTTPS-only (redirect-to-https on the default cache behavior)
#   - TLS 1.2 minimum, server-name indication
#   - WAF associated for per-IP rate limit + UA block + AWS managed rules
#   - Default root object index.html so / resolves to the placeholder
#   - Compression on (Brotli + gzip)
#   - Security headers via AWS-managed policy (HSTS, X-Content-Type-Options, etc.)
#   - Methods cached: GET/HEAD only (no POST/PUT/DELETE on the demo)
#   - Default cache TTL 1 day (placeholder is static; Phase 7 may tune)
#   - v2 access logs delivered to the access-logs bucket at cloudfront/
#
# `aws_cloudfront_response_headers_policy_managed` data source is the
# canonical lookup for AWS-managed response headers policies.
data "aws_cloudfront_response_headers_policy" "security_headers" {
  provider = aws.edge

  name = "Managed-SecurityHeadersPolicy"
}

# AWS-managed cache policy and origin request policy for static S3
# origins — caches by URI only, forwards no headers/cookies/query
# strings. Beats hand-rolling a custom policy.
data "aws_cloudfront_cache_policy" "managed_caching_optimized" {
  provider = aws.edge

  name = "Managed-CachingOptimized"
}

resource "aws_cloudfront_distribution" "this" {
  # checkov:skip=CKV_AWS_310:Custom error responses for 404/403 are deferred until Phase 7's React SPA lands; the placeholder index.html does not need SPA-style fallback. Adding noisy default error pages now would just churn when the SPA arrives.
  # checkov:skip=CKV_AWS_174:Minimum TLS 1.2 + viewer_certificate.cloudfront_default_certificate=false is the locked posture; the cert lives on a custom domain via ACM. Trivy/Checkov sometimes flag this at the wrong layer.
  # checkov:skip=CKV_AWS_86:Standard logging (access_logs block) is intentionally NOT used; this distribution uses v2 CWL Delivery primitives instead because the access-logs bucket has BucketOwnerEnforced on (legacy ACL-based logging is incompatible). See aws_cloudwatch_log_delivery.cloudfront below.
  # checkov:skip=CKV_AWS_374:No geo restriction by design. Recruiter audience for this portfolio demo is globally distributed (US/EU/APAC), and the cost of a false-negative block on a recruiter outweighs any anti-scrape benefit — WAF rate-limit + UA block + IP reputation cover that surface at finer granularity.
  # checkov:skip=CKV2_AWS_47:False positive. The attached WAF web ACL DOES include AWSManagedRulesKnownBadInputsRuleSet (waf.tf priority 4, sid AWSManagedKnownBadInputs) which provides Log4Shell / Spring4Shell coverage. Checkov's cross-resource graph traversal does not always follow web_acl_id back to the WAF resource's rule list, so this check spuriously fails even though the protection is in place.
  enabled         = true
  is_ipv6_enabled = true
  comment         = "Demo edge for ${var.demo_domain} - landing bucket origin (Phase 5b placeholder; Phase 7 swaps content)."
  price_class     = "PriceClass_100"
  http_version    = "http2and3"

  aliases = [var.demo_domain]

  default_root_object = "index.html"

  origin {
    domain_name              = module.landing_bucket.bucket_regional_domain_name
    origin_id                = "landing-s3-origin"
    origin_access_control_id = aws_cloudfront_origin_access_control.landing.id
  }

  default_cache_behavior {
    target_origin_id           = "landing-s3-origin"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = data.aws_cloudfront_cache_policy.managed_caching_optimized.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security_headers.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.this.certificate_arn
    minimum_protocol_version = "TLSv1.2_2021"
    ssl_support_method       = "sni-only"
  }

  web_acl_id = aws_wafv2_web_acl.edge.arn

  tags = {
    Name = "${var.project_name}-edge"
  }

  # The cert needs to be ISSUED before the distribution can attach it,
  # and the OAC needs to exist before the bucket policy referencing it
  # is created (handled via landing.tf's depends_on through the bucket
  # policy resolution). v2 access logs delivery is wired below; the
  # delivery resource depends on this distribution implicitly via its
  # source ARN.
  depends_on = [aws_acm_certificate_validation.this]
}

# v2 access logs delivery: source ON the distribution, destination ON
# the access-logs S3 bucket under cloudfront/, delivery linking them.
# Output format JSON for Athena-friendly downstream querying. Lives
# in us-east-1 (CloudFront delivery primitives are global-but-rooted-
# in-us-east-1, same as the WAF web ACL).

resource "aws_cloudwatch_log_delivery_source" "cloudfront" {
  provider = aws.edge

  name         = "${var.project_name}-cloudfront-source"
  resource_arn = aws_cloudfront_distribution.this.arn
  log_type     = "ACCESS_LOGS"
}

resource "aws_cloudwatch_log_delivery_destination" "cloudfront_s3" {
  provider = aws.edge

  name          = "${var.project_name}-cloudfront-dest"
  output_format = "json"

  delivery_destination_configuration {
    destination_resource_arn = "${module.access_logs_bucket.bucket_arn}/${local.cloudfront_log_s3_prefix}"
  }
}

resource "aws_cloudwatch_log_delivery" "cloudfront" {
  provider = aws.edge

  delivery_source_name     = aws_cloudwatch_log_delivery_source.cloudfront.name
  delivery_destination_arn = aws_cloudwatch_log_delivery_destination.cloudfront_s3.arn

  s3_delivery_configuration {
    suffix_path                 = "AWSLogs/${local.account_id}"
    enable_hive_compatible_path = false
  }
}
