terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.5"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

locals {
  # Repos with immutable OIDC subjects send "repo:owner@ownerId/name@repoId:..." instead of
  # "repo:owner/name:...". The IDs also stop a renamed/recreated repo from inheriting the role.
  github_sub_prefix = (
    var.github_owner_id != "" && var.github_repo_id != ""
    ? "repo:${var.github_org}@${var.github_owner_id}/${var.github_repo}@${var.github_repo_id}"
    : "repo:${var.github_org}/${var.github_repo}"
  )
}

# -----------------------------------------------------------------------------
# 1. Terraform Remote State Storage (S3 + DynamoDB)
# -----------------------------------------------------------------------------
resource "random_string" "bucket_suffix" {
  length  = 6
  special = false
  upper   = false   
}

resource "aws_s3_bucket" "tf_state" {
  bucket        = "${var.s3_bucket_prefix}-${var.aws_region}-${random_string.bucket_suffix.result}"
  force_destroy = false

  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Name        = "Terraform Remote State"
    Environment = "Management"
    ManagedBy   = "TerraAgent-Bootstrap"
  }
}

resource "aws_s3_bucket_versioning" "tf_state_versioning" {
  bucket = aws_s3_bucket.tf_state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tf_state_encryption" {
  bucket = aws_s3_bucket.tf_state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tf_state_block_public" {
  bucket                  = aws_s3_bucket.tf_state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_dynamodb_table" "tf_locks" {
  name         = var.dynamodb_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Name        = "Terraform State Locks"
    Environment = "Management"
    ManagedBy   = "TerraAgent-Bootstrap"
  }
}

# -----------------------------------------------------------------------------
# 2. GitHub OIDC Identity Provider
# -----------------------------------------------------------------------------
# Standard GitHub Actions OIDC thumbprints
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  thumbprint_list = [
    "06d927fecd0a84aeba28aad1d808139470fe95c3",
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f8d264fcd3"
  ]

  tags = {
    Name      = "GitHub-Actions-OIDC-Provider"
    ManagedBy = "TerraAgent-Bootstrap"
  }
}

# -----------------------------------------------------------------------------
# 3. AWS STS Trust Policy & IAM Role for GitHub Actions
# -----------------------------------------------------------------------------
data "aws_iam_policy_document" "github_actions_trust" {
  statement {
    sid     = "GitHubActionsOIDCAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # Restrict assumption strictly to the specified GitHub repository branches & pull requests
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = distinct(concat(
        [
          "repo:${var.github_org}/${var.github_repo}:ref:refs/heads/*",
          "repo:${var.github_org}/${var.github_repo}:pull_request",
        ],
        var.github_owner_id != "" && var.github_repo_id != "" ? [
          "repo:${var.github_org}@${var.github_owner_id}/${var.github_repo}@${var.github_repo_id}:ref:refs/heads/*",
          "repo:${var.github_org}@${var.github_owner_id}/${var.github_repo}@${var.github_repo_id}:pull_request",
        ] : []
      ))
    }
  }
}

resource "aws_iam_role" "github_actions_deployer" {
  name               = var.iam_role_name
  assume_role_policy = data.aws_iam_policy_document.github_actions_trust.json
  description        = "IAM role assumed by GitHub Actions workflows via OIDC for Terraform deployments"

  tags = {
    Name       = var.iam_role_name
    Repository = "${var.github_org}/${var.github_repo}"
    ManagedBy  = "TerraAgent-Bootstrap"
  }
}

# -----------------------------------------------------------------------------
# 4. Permissions Policies for Terraform Deployer Role
# -----------------------------------------------------------------------------
# Permission to access the remote state S3 bucket and DynamoDB lock table
data "aws_iam_policy_document" "tf_state_access" {
  statement {
    sid    = "TerraformStateS3Access"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject"
    ]
    resources = [
      aws_s3_bucket.tf_state.arn,
      "${aws_s3_bucket.tf_state.arn}/*"
    ]
  }

  statement {
    sid    = "TerraformStateDynamoDBLock"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:DeleteItem"
    ]
    resources = [
      aws_dynamodb_table.tf_locks.arn
    ]
  }
}

resource "aws_iam_policy" "tf_state_policy" {
  name        = "TerraformStateBackendAccessPolicy"
  description = "Allows access to the Terraform remote state S3 bucket and DynamoDB locks"
  policy      = data.aws_iam_policy_document.tf_state_access.json
}

resource "aws_iam_role_policy_attachment" "attach_state_policy" {
  role       = aws_iam_role.github_actions_deployer.name
  policy_arn = aws_iam_policy.tf_state_policy.arn
}

# Infrastructure resource management permissions (PowerUser or custom scoped)
resource "aws_iam_role_policy_attachment" "attach_power_user" {
  role       = aws_iam_role.github_actions_deployer.name
  policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"
}
