variable "aws_region" {
  type        = string
  description = "AWS region for deployment"
  default     = "us-east-1"
}

variable "environment" {
  type        = string
  description = "Target deployment environment"
  default     = "production"
}

variable "domain_name" {
  type        = string
  description = "Root domain name for ACM certificate (e.g., example.com)"
  default     = "example.com"
}

variable "app_name" {
  type        = string
  description = "Name of the application or service"
  default     = "terraagent-service"
}
