# Public DNS for the demo. The hosted zone itself
# (Z04568022MZ21HXK15I1D, Mark's apex) is created out-of-band and
# referenced via var.route53_hosted_zone_id; this stack only manages
# records inside it.
#
# Three resource clusters:
#   1. ACM certificate for var.demo_domain (must live in us-east-1
#      because CloudFront only accepts certs from that region).
#   2. DNS validation records (CNAMEs ACM publishes in
#      domain_validation_options) and the validation completion resource
#      that blocks downstream resources until the cert is ISSUED.
#   3. A + AAAA alias records pointing the demo domain at the CloudFront
#      distribution's runtime DNS name (allocated by AWS, opaque).

# ACM cert. DNS validation (not email) is the locked choice — fully
# automated, no inbox dependency. The aliased provider pins this resource
# to us-east-1 regardless of var.aws_region.
resource "aws_acm_certificate" "this" {
  provider = aws.edge

  domain_name       = var.demo_domain
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

# DNS records ACM asks us to publish to prove control. for_each iterates
# the certificate's domain_validation_options (one entry per SAN; here
# just the single SAN). allow_overwrite = true so re-running terraform
# after a manual cleanup of stale validation records doesn't trip on
# pre-existing CNAMEs.
resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.this.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  zone_id         = var.route53_hosted_zone_id
  name            = each.value.name
  type            = each.value.type
  ttl             = 60
  records         = [each.value.record]
  allow_overwrite = true
}

# Blocks downstream resources (the CloudFront distribution) until ACM
# observes the validation records and flips the cert to ISSUED. Without
# this, terraform apply can race ahead and try to attach the cert to a
# distribution before it's valid, which fails.
resource "aws_acm_certificate_validation" "this" {
  provider = aws.edge

  certificate_arn         = aws_acm_certificate.this.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# Public DNS for the demo: A and AAAA aliases pointing at the
# distribution. Aliases are zero-cost and resolve directly to CloudFront
# edge IPs; CNAME at the apex isn't possible per DNS spec but the demo
# is a subdomain anyway, so an A alias is the standard pattern.
#
# evaluate_target_health = false for CloudFront aliases: CloudFront
# distributions don't have a meaningful health check at the alias level
# (Route 53 can't probe an edge network behind an Anycast IP).
resource "aws_route53_record" "demo_a" {
  zone_id = var.route53_hosted_zone_id
  name    = var.demo_domain
  type    = "A"

  alias {
    name                   = aws_cloudfront_distribution.this.domain_name
    zone_id                = aws_cloudfront_distribution.this.hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "demo_aaaa" {
  zone_id = var.route53_hosted_zone_id
  name    = var.demo_domain
  type    = "AAAA"

  alias {
    name                   = aws_cloudfront_distribution.this.domain_name
    zone_id                = aws_cloudfront_distribution.this.hosted_zone_id
    evaluate_target_health = false
  }
}
