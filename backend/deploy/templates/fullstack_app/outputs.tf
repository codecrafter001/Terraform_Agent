output "url" {
  description = "The app's public URL: HTTPS through CloudFront, or the load balancer's HTTP URL without it."
  value       = local.site_url
}

output "cloudfront_distribution_id" {
  description = "CloudFront distribution in front of the app."
  value       = local.cdn ? aws_cloudfront_distribution.app[0].id : null
}

output "alb_dns_name" {
  description = "Load balancer behind CloudFront (only reachable from CloudFront)."
  value       = aws_lb.app.dns_name
}

output "ecr_repository_url" {
  description = "ECR repository for the backend image."
  value       = aws_ecr_repository.app.repository_url
}

output "cluster_name" {
  description = "ECS cluster name."
  value       = aws_ecs_cluster.app.name
}

output "service_name" {
  description = "ECS service name."
  value       = aws_ecs_service.app.name
}

output "pipeline_name" {
  description = "CodePipeline that builds and rolls out the app."
  value       = aws_codepipeline.app.name
}

output "frontend_bucket" {
  description = "S3 bucket holding the built frontend (null without a frontend)."
  value       = local.frontend ? aws_s3_bucket.web[0].bucket : null
}

output "database_endpoint" {
  description = "Database host name (null without a database). The credentials are in database_secret_arn."
  value       = local.db_enabled ? aws_db_instance.main[0].address : null
}

output "database_secret_arn" {
  description = "Secrets Manager secret RDS manages for the database user (ARN only, never the value)."
  value       = local.db_secret_arn
}

output "secrets_to_fill" {
  description = "Environment variable -> Secrets Manager secret name. Each starts empty: set its value, then redeploy the service."
  value       = { for k in sort(var.secret_env_keys) : k => aws_secretsmanager_secret.env[k].name }
}

output "vpc_id" {
  description = "The deployment's own VPC."
  value       = aws_vpc.main.id
}
