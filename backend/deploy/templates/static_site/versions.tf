terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }
}

provider "aws" {
  region = var.region

  # Deployment mode creates new resources, so tagging everything is safe here
  # (unlike adoption code, which must match live resources exactly).
  default_tags {
    tags = {
      "terraagent:managed"       = "true"
      "terraagent:deployment-id" = var.deployment_id
      "terraagent:environment"   = var.environment
    }
  }
}
