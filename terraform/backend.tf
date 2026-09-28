terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Configured to use the S3 remote backend and DynamoDB lock table
  # created by infra/bootstrap-aws-oidc
  # backend "s3" {
  #   bucket         = "REPLACE_WITH_YOUR_STATE_BUCKET"
  #   key            = "environments/production/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "terraagent-tf-locks"
  #   encrypt        = true
  # }
}
