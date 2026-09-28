variable "aws_region" {
  type        = string
  description = "The AWS Region where resources should be deployed."
  default     = "us-east-1"
}

variable "github_org" {
  type        = string
  description = "GitHub Organization or Username owning the repository."
  default     = "codecrafter001"
}

variable "github_repo" {
  type        = string
  description = "GitHub Repository name without org/owner (e.g., Terraform_Agent)."
  default     = "Terraform_Agent"
}

variable "github_owner_id" {
  type        = string
  description = "Numeric GitHub owner ID, for repos using immutable OIDC subjects (gh api repos/OWNER/REPO/actions/oidc/customization/sub). Empty = classic subject."
  default     = "145287568"
}

variable "github_repo_id" {
  type        = string
  description = "Numeric GitHub repository ID, for repos using immutable OIDC subjects. Empty = classic subject."
  default     = "1373587293"
}

variable "iam_role_name" {
  type        = string
  description = "Name for the IAM role assumed by GitHub Actions."
  default     = "GitHubActions-TerraformDeployer"
}

variable "s3_bucket_prefix" {
  type        = string
  description = "Prefix for the S3 bucket used for Terraform state storage."
  default     = "terraagent-tf-state"
}

variable "dynamodb_table_name" {
  type        = string
  description = "Name of the DynamoDB table used for Terraform state locking."
  default     = "terraagent-tf-locks"
}
