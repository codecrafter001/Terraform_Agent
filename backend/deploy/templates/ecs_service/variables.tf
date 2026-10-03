variable "deployment_id" {
  description = "TerraAgent deployment id; prefixes every resource name."
  type        = string
  validation {
    condition     = can(regex("^dep-[0-9a-f]{12}$", var.deployment_id))
    error_message = "deployment_id must look like dep-<12 hex characters>."
  }
}

variable "region" {
  description = "AWS region for the ECS cluster and service."
  type        = string
  validation {
    condition     = can(regex("^[a-z]{2}(-gov)?-[a-z]+-[0-9]$", var.region))
    error_message = "region must be an AWS region name."
  }
}

variable "environment" {
  description = "Free-form environment label, used only as a tag."
  type        = string
  validation {
    condition     = can(regex("^[A-Za-z0-9_-]{1,32}$", var.environment))
    error_message = "environment must be 1-32 letters, digits, '-' or '_'."
  }
}

variable "container_port" {
  description = "Port the container application listens on."
  type        = number
  default     = 8080
  validation {
    condition     = var.container_port >= 1 && var.container_port <= 65535
    error_message = "container_port must be between 1 and 65535."
  }
}

variable "cpu" {
  description = "Fargate CPU units (256, 512, 1024, 2048, 4096)."
  type        = number
  default     = 256
  validation {
    condition     = contains([256, 512, 1024, 2048, 4096], var.cpu)
    error_message = "cpu must be a valid Fargate CPU size (256, 512, 1024, 2048, 4096)."
  }
}

variable "memory_mb" {
  description = "Fargate memory in MB (512, 1024, 2048, 4096, 8192)."
  type        = number
  default     = 512
  validation {
    condition     = contains([512, 1024, 2048, 4096, 8192], var.memory_mb)
    error_message = "memory_mb must be a valid Fargate memory allocation."
  }
}

variable "desired_count" {
  description = "Number of ECS task replicas to run."
  type        = number
  default     = 1
  validation {
    condition     = var.desired_count >= 1 && var.desired_count <= 10
    error_message = "desired_count must be between 1 and 10."
  }
}

variable "image_tag" {
  description = "Container image tag in ECR."
  type        = string
  default     = "latest"
  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]{1,128}$", var.image_tag))
    error_message = "image_tag must be 1-128 characters (letters, digits, _, ., -)."
  }
}

variable "certificate_arn" {
  description = "Optional ACM certificate ARN for an HTTPS listener on the ALB (HTTP then redirects to HTTPS)."
  type        = string
  default     = null
  validation {
    condition     = var.certificate_arn == null || can(regex("^arn:aws[a-z-]*:acm:[a-z0-9-]+:[0-9]{12}:certificate/[A-Za-z0-9-]+$", var.certificate_arn))
    error_message = "certificate_arn must be an ACM certificate ARN."
  }
}

variable "log_retention_days" {
  description = "CloudWatch logs retention period in days."
  type        = number
  default     = 30
  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365], var.log_retention_days)
    error_message = "log_retention_days must be a CloudWatch retention value (1-365)."
  }
}

variable "permissions_boundary_arn" {
  description = "Permissions boundary for every role this stack creates; defaults to the account's TerraAgentWorkloadBoundary."
  type        = string
  default     = null
  validation {
    condition     = var.permissions_boundary_arn == null || can(regex("^arn:aws[a-z-]*:iam::[0-9]{12}:policy/", var.permissions_boundary_arn))
    error_message = "permissions_boundary_arn must be an IAM policy ARN."
  }
}

variable "vpc_cidr" {
  description = "CIDR block of the deployment's own VPC."
  type        = string
  default     = "10.42.0.0/16"
  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0)) && tonumber(split("/", var.vpc_cidr)[1]) <= 20
    error_message = "vpc_cidr must be an IPv4 CIDR block of /20 or larger."
  }
}

variable "source_file" {
  description = "Path (relative to this module) of the source.zip CodeBuild builds the image from."
  type        = string
  validation {
    condition     = can(regex("^artifacts/[A-Za-z0-9_.-]+\\.zip$", var.source_file))
    error_message = "source_file must be a zip under artifacts/."
  }
}

variable "health_check_path" {
  description = "Path the load balancer health check requests."
  type        = string
  default     = "/"
  validation {
    condition     = can(regex("^/[A-Za-z0-9_./-]{0,200}$", var.health_check_path))
    error_message = "health_check_path must start with / and contain only URL path characters."
  }
}
