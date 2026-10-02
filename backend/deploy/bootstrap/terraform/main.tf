terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "state" {
  bucket        = var.state_bucket_name
  force_destroy = false

  tags = {
    "terraagent:managed" = "true"
    "Name"               = "terraagent-state"
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket = aws_s3_bucket.state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# -----------------------------------------------------------------------------
# Workload Permissions Boundary
# -----------------------------------------------------------------------------
resource "aws_iam_policy" "workload_boundary" {
  name        = "TerraAgentWorkloadBoundary"
  description = "Permissions boundary capping IAM roles created by TerraAgent for application workloads"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowBasicLogsAndMetrics"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "cloudwatch:PutMetricData"
        ]
        Resource = "*"
      },
      {
        Sid    = "AllowScopedResourceAccess"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:ListBucket",
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:Query",
          "dynamodb:Scan"
        ]
        Resource = [
          "arn:aws:s3:::terraagent-*",
          "arn:aws:dynamodb:*:*:table/terraagent-*"
        ]
      },
      {
        Sid    = "DenyPrivilegeEscalation"
        Effect = "Deny"
        Action = [
          "iam:*",
          "organizations:*",
          "account:*"
        ]
        Resource = "*"
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# Plan Role (Read-only)
# -----------------------------------------------------------------------------
resource "aws_iam_role" "plan" {
  name                 = "TerraAgentDeployPlan"
  max_session_duration = 3600

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          AWS = var.terraagent_principal_arn
        }
        Action = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "sts:ExternalId" = var.external_id
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "plan_policy" {
  name = "TerraAgentPlanPolicy"
  role = aws_iam_role.plan.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowReadServices"
        Effect = "Allow"
        Action = [
          "s3:Get*",
          "s3:List*",
          "lambda:Get*",
          "lambda:List*",
          "apigateway:GET",
          "cloudfront:Get*",
          "cloudfront:List*",
          "logs:Describe*",
          "logs:Get*",
          "iam:Get*",
          "iam:List*",
          "ecs:Describe*",
          "ecs:List*",
          "ecr:Describe*",
          "ecr:Get*",
          "ecr:List*",
          "codebuild:BatchGet*",
          "codebuild:List*",
          "elasticloadbalancing:Describe*",
          "ec2:Describe*"
        ]
        Resource = "*"
      },
      {
        Sid    = "AllowStateReadAndLock"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket",
          "s3:PutObject",
          "s3:DeleteObject"
        ]
        Resource = [
          aws_s3_bucket.state.arn,
          "${aws_s3_bucket.state.arn}/*"
        ]
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# Apply Role (Scoped Mutation)
# -----------------------------------------------------------------------------
resource "aws_iam_role" "apply" {
  name                 = "TerraAgentDeployApply"
  max_session_duration = 3600

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          AWS = var.terraagent_principal_arn
        }
        Action = [
          "sts:AssumeRole",
          "sts:TagSession",
          "sts:SetSourceIdentity"
        ]
        Condition = {
          StringEquals = {
            "sts:ExternalId" = var.external_id
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "apply_policy" {
  name = "TerraAgentApplyScopedPolicy"
  role = aws_iam_role.apply.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowScopedInfrastructure"
        Effect = "Allow"
        Action = [
          "s3:*",
          "lambda:*",
          "apigateway:*",
          "cloudfront:*",
          "logs:*",
          "ecs:*",
          "ecr:*",
          "codebuild:*",
          "elasticloadbalancing:*",
          "ec2:Describe*",
          "ec2:CreateSecurityGroup",
          "ec2:AuthorizeSecurityGroupIngress",
          "ec2:AuthorizeSecurityGroupEgress",
          "ec2:RevokeSecurityGroupIngress",
          "ec2:RevokeSecurityGroupEgress",
          "ec2:DeleteSecurityGroup",
          "ec2:CreateTags"
        ]
        Resource = "*"
        Condition = {
          StringLike = {
            "aws:ResourceTag/terraagent:managed" = "true"
          }
        }
      },
      {
        Sid    = "AllowCreateTaggedResources"
        Effect = "Allow"
        Action = [
          "s3:CreateBucket",
          "s3:PutBucket*",
          "lambda:CreateFunction",
          "lambda:TagResource",
          "apigateway:POST",
          "cloudfront:CreateDistribution",
          "logs:CreateLogGroup",
          "ecr:CreateRepository",
          "ecs:CreateCluster",
          "elasticloadbalancing:CreateLoadBalancer",
          "elasticloadbalancing:CreateTargetGroup"
        ]
        Resource = "*"
      },
      {
        Sid    = "AllowIAMRolesWithBoundary"
        Effect = "Allow"
        Action = [
          "iam:CreateRole",
          "iam:PutRolePolicy",
          "iam:AttachRolePolicy",
          "iam:DeleteRolePolicy",
          "iam:DetachRolePolicy",
          "iam:DeleteRole",
          "iam:TagRole",
          "iam:GetRole",
          "iam:GetRolePolicy",
          "iam:ListRolePolicies",
          "iam:ListAttachedRolePolicies"
        ]
        Resource = "arn:aws:iam::*:role/terraagent/*"
        Condition = {
          StringEquals = {
            "iam:PermissionsBoundary" = aws_iam_policy.workload_boundary.arn
          }
        }
      },
      {
        Sid      = "AllowPassRoleToWorkloads"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = "arn:aws:iam::*:role/terraagent/*"
        Condition = {
          StringEquals = {
            "iam:PassedToService" = [
              "lambda.amazonaws.com",
              "ecs-tasks.amazonaws.com",
              "codebuild.amazonaws.com"
            ]
          }
        }
      },
      {
        Sid    = "AllowStateManagement"
        Effect = "Allow"
        Action = "s3:*"
        Resource = [
          aws_s3_bucket.state.arn,
          "${aws_s3_bucket.state.arn}/*"
        ]
      },
      {
        Sid    = "ExplicitSecurityDenies"
        Effect = "Deny"
        Action = [
          "iam:CreateUser",
          "iam:CreateAccessKey",
          "iam:CreateLoginProfile",
          "organizations:*",
          "account:*",
          "kms:ScheduleKeyDeletion"
        ]
        Resource = "*"
      }
    ]
  })
}
