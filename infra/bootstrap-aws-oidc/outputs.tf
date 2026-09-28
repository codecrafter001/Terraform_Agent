output "aws_region" {
  description = "The AWS region used for deployment."
  value       = var.aws_region
}

output "github_actions_role_arn" {
  description = "IAM Role ARN to configure as AWS_DEPLOY_ROLE_ARN secret in GitHub Actions."
  value       = aws_iam_role.github_actions_deployer.arn
}

output "tf_state_s3_bucket" {
  description = "S3 bucket name created for Terraform remote state."
  value       = aws_s3_bucket.tf_state.bucket
}

output "tf_locks_dynamodb_table" {
  description = "DynamoDB table name created for Terraform state locking."
  value       = aws_dynamodb_table.tf_locks.name
}

output "backend_config_snippet" {
  description = "Sample backend.tf configuration snippet for your terraform codebase."
  value       = <<-EOT
    terraform {
      backend "s3" {
        bucket         = "${aws_s3_bucket.tf_state.bucket}"
        key            = "environments/production/terraform.tfstate"
        region         = "${var.aws_region}"
        dynamodb_table = "${aws_dynamodb_table.tf_locks.name}"
        encrypt        = true
      }
    }
  EOT
}
