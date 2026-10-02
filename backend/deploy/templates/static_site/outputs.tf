output "url" {
  description = "Public HTTPS URL of the site."
  value       = "https://${aws_cloudfront_distribution.site.domain_name}"
}

output "bucket" {
  value = aws_s3_bucket.site.id
}

output "distribution_id" {
  value = aws_cloudfront_distribution.site.id
}
