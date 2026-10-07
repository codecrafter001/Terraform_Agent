output "url" {
  description = "HTTP or HTTPS endpoint of the application load balancer."
  value       = var.certificate_arn != null ? "https://${aws_lb.app.dns_name}" : "http://${aws_lb.app.dns_name}"
}

output "alb_dns_name" {
  description = "DNS name of the Application Load Balancer."
  value       = aws_lb.app.dns_name
}

output "ecr_repository_url" {
  description = "ECR Repository URL for container images."
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

output "codebuild_project_name" {
  description = "CodeBuild project name."
  value       = aws_codebuild_project.builder.name
}

output "pipeline_name" {
  description = "CodePipeline that builds the image and rolls the service."
  value       = aws_codepipeline.app.name
}

output "vpc_id" {
  description = "The deployment's own VPC."
  value       = aws_vpc.main.id
}
