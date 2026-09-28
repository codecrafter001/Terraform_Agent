output "acm_certificate_arn" {
  description = "ARN of the provisioned ACM SSL/TLS certificate"
  value       = aws_acm_certificate.cert.arn
}

output "acm_domain_validation_options" {
  description = "Domain validation records required for DNS verification"
  value       = aws_acm_certificate.cert.domain_validation_options
}
