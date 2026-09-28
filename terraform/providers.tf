provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      ManagedBy   = "Terraform"
      Provisioner = "TerraAgent"
      Environment = var.environment
    }
  }
}
