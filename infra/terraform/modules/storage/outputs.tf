output "bucket_id" {
  description = "Bucket name (id)."
  value       = aws_s3_bucket.this.id
}

output "bucket_arn" {
  description = "Bucket ARN."
  value       = aws_s3_bucket.this.arn
}

output "bucket_regional_domain_name" {
  description = "Regional domain name (used in CloudFront origins, presigned URLs)."
  value       = aws_s3_bucket.this.bucket_regional_domain_name
}
