# TerraAgent customer bootstrap (Terraform flavour of ../cloudformation.yaml):
# the Terraform state bucket, the read-only Plan role, the scoped Apply role and
# the permissions boundary every workload role TerraAgent creates must carry.
#
# Scoping model for the Apply role (a per-deployment session policy narrows it
# further, see backend/deploy/sts.py::apply_session_policy):
# - resources whose ARN carries the terraagent- name prefix;
# - ID-named resources (EC2 networking, CloudFront, task definitions) only when
#   created with, or already carrying, the terraagent:managed tag. Tags can be
#   written only at creation, so an existing resource can't be tagged into scope.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "region" {
  description = "Region for the state bucket (deployments can target any region)."
  type        = string
  default     = "us-east-1"
}

variable "state_bucket_name" {
  description = "Globally unique name for the Terraform remote state bucket."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9.-]{3,63}$", var.state_bucket_name))
    error_message = "state_bucket_name must be a valid S3 bucket name."
  }
}

variable "terraagent_principal_arn" {
  description = "ARN of TerraAgent's AWS role or user that assumes the Plan and Apply roles."
  type        = string
  validation {
    condition     = can(regex("^arn:aws[a-z0-9-]*:iam::[0-9]{12}:(role|user)/.+$", var.terraagent_principal_arn))
    error_message = "terraagent_principal_arn must be an IAM role or user ARN."
  }
}

variable "external_id" {
  description = "Tenant-specific ExternalId from TerraAgent Settings -> Deploy Targets."
  type        = string
  sensitive   = true
  validation {
    condition     = length(var.external_id) >= 16 && length(var.external_id) <= 128
    error_message = "external_id must be 16-128 characters."
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
          "cloudwatch:PutMetricData",
          "ecr:GetAuthorizationToken",
          "cloudfront:CreateInvalidation",
          "cloudfront:ListDistributions"
        ]
        Resource = "*"
      },
      {
        Sid    = "AllowScopedResourceAccess"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:GetObjectVersion",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
          "s3:GetBucketLocation",
          "s3:GetBucketVersioning",
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
        Sid    = "AllowContainerPipeline"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:CompleteLayerUpload",
          "ecr:DescribeImages",
          "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
          "ecs:UpdateService",
          "ecs:DescribeServices",
          "codebuild:StartBuild",
          "codebuild:BatchGetBuilds",
          "codepipeline:StartPipelineExecution"
        ]
        Resource = [
          "arn:aws:ecr:*:*:repository/terraagent-*",
          "arn:aws:ecs:*:*:service/terraagent-*/*",
          "arn:aws:codebuild:*:*:project/terraagent-*",
          "arn:aws:codepipeline:*:*:terraagent-*"
        ]
      },
      {
        # ECS execution roles read the app's secrets once, at task start.
        Sid    = "AllowReadingAppSecretsAtTaskStart"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = [
          "arn:aws:secretsmanager:*:*:secret:terraagent-*",
          "arn:aws:secretsmanager:*:*:secret:rds!*"
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
          "cloudfront:Describe*",
          "logs:Describe*",
          "logs:Get*",
          "logs:List*",
          "iam:Get*",
          "iam:List*",
          "ecs:Describe*",
          "ecs:List*",
          "ecr:Describe*",
          "ecr:Get*",
          "ecr:List*",
          "codebuild:BatchGet*",
          "codebuild:List*",
          "codepipeline:Get*",
          "codepipeline:List*",
          "events:Describe*",
          "events:List*",
          "elasticloadbalancing:Describe*",
          "ec2:Describe*",
          "rds:Describe*",
          "rds:ListTagsForResource",
          "secretsmanager:DescribeSecret",
          "secretsmanager:GetResourcePolicy",
          "elasticache:Describe*",
          "elasticache:List*",
          "application-autoscaling:Describe*"
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
        Sid    = "AllowNamespacedResources"
        Effect = "Allow"
        Action = [
          "s3:*",
          "lambda:*",
          "logs:*",
          "ecs:*",
          "ecr:*",
          "codebuild:*",
          "codepipeline:*",
          "events:*",
          "elasticloadbalancing:*",
          "rds:*",
          "secretsmanager:*",
          "cloudfront:*",
          "elasticache:*"
        ]
        Resource = "arn:aws:*:*:*:*terraagent-*"
      },
      {
        Sid    = "AllowTaggedCreate"
        Effect = "Allow"
        Action = [
          "ec2:CreateVpc",
          "ec2:CreateSubnet",
          "ec2:CreateInternetGateway",
          "ec2:CreateRouteTable",
          "ec2:CreateSecurityGroup",
          "cloudfront:CreateDistribution",
          "cloudfront:CreateDistributionWithTags",
          "ecs:RegisterTaskDefinition",
          "apigateway:POST",
          "application-autoscaling:RegisterScalableTarget",
          "application-autoscaling:TagResource"
        ]
        Resource = "*"
        Condition = {
          StringEquals = { "aws:RequestTag/terraagent:managed" = "true" }
        }
      },
      {
        Sid      = "AllowTaggingOnCreate"
        Effect   = "Allow"
        Action   = ["ec2:CreateTags"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "ec2:CreateAction" = ["CreateVpc", "CreateSubnet", "CreateInternetGateway", "CreateRouteTable", "CreateSecurityGroup"]
          }
        }
      },
      {
        Sid      = "AllowEcsTaggingOnCreate"
        Effect   = "Allow"
        Action   = ["ecs:TagResource"]
        Resource = "*"
        Condition = {
          StringEquals = { "ecs:CreateAction" = "RegisterTaskDefinition" }
        }
      },
      {
        Sid    = "AllowTaggedManage"
        Effect = "Allow"
        Action = [
          "ec2:*",
          "cloudfront:*",
          "ecs:*",
          "apigateway:*",
          "application-autoscaling:*"
        ]
        Resource = "*"
        Condition = {
          StringEquals = { "aws:ResourceTag/terraagent:managed" = "true" }
        }
      },
      {
        Sid    = "AllowReadAndHelpers"
        Effect = "Allow"
        Action = [
          "ec2:Describe*",
          "ecs:Describe*",
          "ecs:List*",
          "ecs:DeregisterTaskDefinition",
          "elasticloadbalancing:Describe*",
          "rds:Describe*",
          "cloudfront:Get*",
          "cloudfront:List*",
          "cloudfront:Describe*",
          "cloudfront:CreateOriginAccessControl",
          "cloudfront:GetOriginAccessControl",
          "cloudfront:UpdateOriginAccessControl",
          "cloudfront:DeleteOriginAccessControl",
          "kms:DescribeKey",
          "elasticache:Describe*",
          "application-autoscaling:Describe*"
        ]
        Resource = "*"
      },
      {
        # Rules always come with their security group, which the tag statements check.
        Sid      = "AllowSecurityGroupRules"
        Effect   = "Allow"
        Action   = ["ec2:*SecurityGroup*"]
        Resource = "arn:aws:ec2:*:*:security-group-rule/*"
      },
      {
        Sid      = "AllowServiceLinkedRoles"
        Effect   = "Allow"
        Action   = "iam:CreateServiceLinkedRole"
        Resource = "arn:aws:iam::*:role/aws-service-role/*"
        Condition = {
          StringEquals = {
            "iam:AWSServiceName" = [
              "ecs.amazonaws.com",
              "elasticloadbalancing.amazonaws.com",
              "rds.amazonaws.com",
              "elasticache.amazonaws.com",
              "ecs.application-autoscaling.amazonaws.com"
            ]
          }
        }
      },
      {
        # The database password secret RDS creates (manage_master_user_password); never readable.
        Sid    = "AllowRdsManagedSecret"
        Effect = "Allow"
        Action = [
          "secretsmanager:CreateSecret",
          "secretsmanager:TagResource",
          "secretsmanager:RotateSecret",
          "secretsmanager:DeleteSecret",
          "secretsmanager:DescribeSecret"
        ]
        Resource = "arn:aws:secretsmanager:*:*:secret:rds!*"
      },
      {
        Sid    = "AllowIAMRolesWithBoundary"
        Effect = "Allow"
        Action = [
          "iam:CreateRole",
          "iam:PutRolePolicy",
          "iam:AttachRolePolicy",
          "iam:DeleteRolePolicy",
          "iam:DetachRolePolicy"
        ]
        Resource = "arn:aws:iam::*:role/terraagent/*"
        Condition = {
          StringEquals = {
            "iam:PermissionsBoundary" = aws_iam_policy.workload_boundary.arn
          }
        }
      },
      {
        Sid    = "AllowIAMRoleLifecycle"
        Effect = "Allow"
        Action = [
          "iam:DeleteRole",
          "iam:TagRole",
          "iam:UntagRole",
          "iam:GetRole",
          "iam:GetRolePolicy",
          "iam:ListRolePolicies",
          "iam:ListAttachedRolePolicies",
          "iam:ListInstanceProfilesForRole",
          "iam:ListRoleTags"
        ]
        Resource = "arn:aws:iam::*:role/terraagent/*"
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
              "codebuild.amazonaws.com",
              "codepipeline.amazonaws.com",
              "events.amazonaws.com"
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
          "iam:PutRolePermissionsBoundary",
          "iam:DeleteRolePermissionsBoundary",
          "secretsmanager:GetSecretValue",
          "secretsmanager:PutSecretValue",
          "organizations:*",
          "account:*",
          "kms:ScheduleKeyDeletion"
        ]
        Resource = "*"
      }
    ]
  })
}

output "plan_role_arn" {
  description = "ARN of the TerraAgent Plan role (paste into Settings -> Deploy Targets)."
  value       = aws_iam_role.plan.arn
}

output "apply_role_arn" {
  description = "ARN of the TerraAgent Apply role."
  value       = aws_iam_role.apply.arn
}

output "permissions_boundary_arn" {
  description = "Boundary every workload role TerraAgent creates must carry."
  value       = aws_iam_policy.workload_boundary.arn
}

output "state_bucket_name" {
  description = "Terraform state bucket."
  value       = aws_s3_bucket.state.bucket
}
