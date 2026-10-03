# Full stack: one CloudFront URL in front of an S3-hosted frontend (optional)
# and an ECS Fargate backend behind a load balancer, with an optional RDS
# PostgreSQL/MySQL database in private subnets - all in the deployment's own VPC.
#
# Everything is built inside the customer's account: Terraform uploads
# source.zip to S3, which starts a CodePipeline whose CodeBuild step builds and
# pushes the backend image, builds the frontend and syncs it to S3, then rolls
# the service. One approved apply does it all; TerraAgent never calls a build or
# deploy API itself.
#
# Secrets: RDS creates and stores the database password in Secrets Manager
# (manage_master_user_password), and every application environment variable gets
# an empty Secrets Manager secret for the customer to fill in. Terraform never
# manages a secret value, so no secret ever reaches the plan, the state or
# TerraAgent.

locals {
  name     = "terraagent-${var.deployment_id}"
  azs      = slice(data.aws_availability_zones.available.names, 0, 2)
  boundary = coalesce(var.permissions_boundary_arn, "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:policy/TerraAgentWorkloadBoundary")
  registry = split("/", aws_ecr_repository.app.repository_url)[0]
  frontend = var.frontend_enabled
  # A separately built frontend is served from a private S3 bucket, which needs
  # CloudFront in front of it; a backend-only app can skip it (faster, ALB URL).
  cdn         = var.cdn_enabled || var.frontend_enabled
  db_enabled  = var.database_engine != null
  db_port     = var.database_engine == "postgres" ? 5432 : 3306
  db_name     = "app"
  site_url    = local.cdn ? "https://${aws_cloudfront_distribution.app[0].domain_name}" : "http://${aws_lb.app.dns_name}"
  service_arn = "arn:${data.aws_partition.current.partition}:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:service/${local.name}/${local.name}"

  db_secret_arn    = local.db_enabled ? aws_db_instance.main[0].master_user_secret[0].secret_arn : null
  env_secret_arns  = [for k in sort(var.secret_env_keys) : aws_secretsmanager_secret.env[k].arn]
  readable_secrets = compact(concat(local.env_secret_arns, [local.db_secret_arn]))

  container_environment = concat(
    [
      { name = "PORT", value = tostring(var.container_port) },
      { name = "HOST", value = "0.0.0.0" },
      { name = "NODE_ENV", value = "production" },
    ],
    local.db_enabled ? [
      { name = "DB_HOST", value = aws_db_instance.main[0].address },
      { name = "DB_PORT", value = tostring(local.db_port) },
      { name = "DB_NAME", value = local.db_name },
      { name = "DB_URL_SCHEME", value = coalesce(var.database_url_scheme, var.database_engine == "postgres" ? "postgresql" : "mysql") },
      { name = "TERRAAGENT_RUN_MIGRATIONS", value = tostring(var.run_migrations) },
    ] : [],
  )
  container_secrets = concat(
    [for k in sort(var.secret_env_keys) : { name = k, valueFrom = aws_secretsmanager_secret.env[k].arn }],
    local.db_enabled ? [
      { name = "DB_USER", valueFrom = "${local.db_secret_arn}:username::" },
      { name = "DB_PASSWORD", valueFrom = "${local.db_secret_arn}:password::" },
    ] : [],
  )
}

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_viewer_except_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

# --- Network: dedicated VPC, public subnets for the ALB and tasks, private ones
# --- for the database (no NAT gateway: the database needs no internet) -------

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = local.name
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
}

resource "aws_subnet" "public" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index)

  tags = {
    Name = "${local.name}-public-${count.index}"
  }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 10)

  tags = {
    Name = "${local.name}-private-${count.index}"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# --- Source bucket: source.zip in, pipeline artifacts out ---------------------

resource "aws_s3_bucket" "source" {
  bucket        = "${local.name}-src"
  force_destroy = true
}

resource "aws_s3_bucket_ownership_controls" "source" {
  bucket = aws_s3_bucket.source.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "source" {
  bucket                  = aws_s3_bucket.source.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "source" {
  bucket = aws_s3_bucket.source.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "source" {
  bucket = aws_s3_bucket.source.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_notification" "source" {
  bucket      = aws_s3_bucket.source.id
  eventbridge = true
}

resource "aws_s3_object" "source" {
  bucket      = aws_s3_bucket.source.id
  key         = "source.zip"
  source      = "${path.module}/${var.source_file}"
  source_hash = filemd5("${path.module}/${var.source_file}")

  depends_on = [aws_s3_bucket_versioning.source, aws_s3_bucket_server_side_encryption_configuration.source]
}

# --- Frontend bucket (private; CloudFront reads it through origin access control)

resource "aws_s3_bucket" "web" {
  count         = local.frontend ? 1 : 0
  bucket        = "${local.name}-web"
  force_destroy = true
}

resource "aws_s3_bucket_ownership_controls" "web" {
  count  = local.frontend ? 1 : 0
  bucket = aws_s3_bucket.web[0].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "web" {
  count                   = local.frontend ? 1 : 0
  bucket                  = aws_s3_bucket.web[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "web" {
  count  = local.frontend ? 1 : 0
  bucket = aws_s3_bucket.web[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

data "aws_iam_policy_document" "web_bucket" {
  count = local.frontend ? 1 : 0

  statement {
    sid       = "CloudFrontRead"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.web[0].arn}/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.app[0].arn]
    }
  }
}

resource "aws_s3_bucket_policy" "web" {
  count  = local.frontend ? 1 : 0
  bucket = aws_s3_bucket.web[0].id
  policy = data.aws_iam_policy_document.web_bucket[0].json

  depends_on = [aws_s3_bucket_public_access_block.web]
}

# --- ECR -----------------------------------------------------------------------

resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep the last 10 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 10
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

# --- Logs ----------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "ecs" {
  name              = "/aws/ecs/${local.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "codebuild" {
  name              = "/aws/codebuild/${local.name}"
  retention_in_days = var.log_retention_days
}

# --- Database (optional) ----------------------------------------------------------

resource "aws_db_subnet_group" "main" {
  count      = local.db_enabled ? 1 : 0
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}

# PostgreSQL 16 on RDS rejects non-TLS connections by default; most app drivers
# don't trust the RDS CA out of the box, so TLS is optional inside the private VPC.
resource "aws_db_parameter_group" "postgres" {
  count  = var.database_engine == "postgres" ? 1 : 0
  name   = local.name
  family = "postgres16"

  parameter {
    name  = "rds.force_ssl"
    value = "0"
  }
}

resource "aws_db_instance" "main" {
  count                       = local.db_enabled ? 1 : 0
  identifier                  = local.name
  engine                      = var.database_engine == "postgres" ? "postgres" : "mysql"
  engine_version              = var.database_engine == "postgres" ? "16" : "8.0"
  instance_class              = var.db_instance_class
  allocated_storage           = var.db_allocated_storage_gb
  max_allocated_storage       = var.db_allocated_storage_gb * 5
  storage_type                = "gp3"
  storage_encrypted           = true
  db_name                     = local.db_name
  username                    = "app_admin"
  manage_master_user_password = true
  db_subnet_group_name        = aws_db_subnet_group.main[0].name
  parameter_group_name        = var.database_engine == "postgres" ? aws_db_parameter_group.postgres[0].name : null
  vpc_security_group_ids      = [aws_security_group.db[0].id]
  publicly_accessible         = false
  multi_az                    = var.db_multi_az
  backup_retention_period     = var.db_backup_retention_days
  copy_tags_to_snapshot       = true
  auto_minor_version_upgrade  = true
  apply_immediately           = true
  deletion_protection         = false
  skip_final_snapshot         = !var.db_final_snapshot
  final_snapshot_identifier   = "${local.name}-final"
}

# --- Application secrets: created empty, filled in by the customer ---------------

resource "aws_secretsmanager_secret" "env" {
  for_each                = toset(var.secret_env_keys)
  name                    = "${local.name}/${each.key}"
  description             = "${each.key} for ${local.name}. Set the value in the AWS console; TerraAgent never reads it."
  recovery_window_in_days = 0
}

# --- IAM (every role under /terraagent/ with the workload boundary) -------------

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name                 = "${local.name}-exec"
  path                 = "/terraagent/"
  assume_role_policy   = data.aws_iam_policy_document.ecs_assume.json
  permissions_boundary = local.boundary
}

data "aws_iam_policy_document" "execution_policy" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.ecs.arn}:*"]
  }

  statement {
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    actions   = ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"]
    resources = [aws_ecr_repository.app.arn]
  }

  # ECS reads the secrets once, at task start, to inject them as environment variables.
  dynamic "statement" {
    for_each = length(local.readable_secrets) > 0 ? [1] : []
    content {
      actions   = ["secretsmanager:GetSecretValue"]
      resources = local.readable_secrets
    }
  }
}

resource "aws_iam_role_policy" "execution" {
  name   = "execution"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_policy.json
}

# The application's own role: no permissions unless the customer adds them.
resource "aws_iam_role" "task" {
  name                 = "${local.name}-task"
  path                 = "/terraagent/"
  assume_role_policy   = data.aws_iam_policy_document.ecs_assume.json
  permissions_boundary = local.boundary
}

data "aws_iam_policy_document" "codebuild_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codebuild.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "codebuild" {
  name                 = "${local.name}-cb"
  path                 = "/terraagent/"
  assume_role_policy   = data.aws_iam_policy_document.codebuild_assume.json
  permissions_boundary = local.boundary
}

data "aws_iam_policy_document" "codebuild_policy" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.codebuild.arn}:*"]
  }

  statement {
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [aws_ecr_repository.app.arn]
  }

  statement {
    actions   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"]
    resources = ["${aws_s3_bucket.source.arn}/*"]
  }

  statement {
    actions   = ["s3:GetBucketLocation", "s3:GetBucketVersioning", "s3:ListBucket"]
    resources = [aws_s3_bucket.source.arn]
  }

  statement {
    actions   = ["ecs:UpdateService", "ecs:DescribeServices"]
    resources = [local.service_arn]
  }

  dynamic "statement" {
    for_each = local.frontend ? [1] : []
    content {
      actions   = ["s3:PutObject", "s3:DeleteObject", "s3:GetObject"]
      resources = ["${aws_s3_bucket.web[0].arn}/*"]
    }
  }

  dynamic "statement" {
    for_each = local.frontend ? [1] : []
    content {
      actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
      resources = [aws_s3_bucket.web[0].arn]
    }
  }

  # Not tied to this distribution's ARN: that would make the build wait for
  # CloudFront. Invalidations are harmless; the build finds its distribution by comment.
  dynamic "statement" {
    for_each = local.frontend ? [1] : []
    content {
      actions   = ["cloudfront:CreateInvalidation"]
      resources = ["arn:${data.aws_partition.current.partition}:cloudfront::${data.aws_caller_identity.current.account_id}:distribution/*"]
    }
  }

  dynamic "statement" {
    for_each = local.frontend ? [1] : []
    content {
      actions   = ["cloudfront:ListDistributions"]
      resources = ["*"]
    }
  }
}

resource "aws_iam_role_policy" "codebuild" {
  name   = "codebuild"
  role   = aws_iam_role.codebuild.id
  policy = data.aws_iam_policy_document.codebuild_policy.json
}

data "aws_iam_policy_document" "codepipeline_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codepipeline.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "codepipeline" {
  name                 = "${local.name}-cp"
  path                 = "/terraagent/"
  assume_role_policy   = data.aws_iam_policy_document.codepipeline_assume.json
  permissions_boundary = local.boundary
}

data "aws_iam_policy_document" "codepipeline_policy" {
  statement {
    actions   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"]
    resources = ["${aws_s3_bucket.source.arn}/*"]
  }

  statement {
    actions   = ["s3:GetBucketLocation", "s3:GetBucketVersioning", "s3:ListBucket"]
    resources = [aws_s3_bucket.source.arn]
  }

  statement {
    actions   = ["codebuild:StartBuild", "codebuild:BatchGetBuilds"]
    resources = [aws_codebuild_project.builder.arn]
  }
}

resource "aws_iam_role_policy" "codepipeline" {
  name   = "codepipeline"
  role   = aws_iam_role.codepipeline.id
  policy = data.aws_iam_policy_document.codepipeline_policy.json
}

data "aws_iam_policy_document" "events_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "events" {
  name                 = "${local.name}-ev"
  path                 = "/terraagent/"
  assume_role_policy   = data.aws_iam_policy_document.events_assume.json
  permissions_boundary = local.boundary
}

data "aws_iam_policy_document" "events_policy" {
  statement {
    actions   = ["codepipeline:StartPipelineExecution"]
    resources = [aws_codepipeline.app.arn]
  }
}

resource "aws_iam_role_policy" "events" {
  name   = "events"
  role   = aws_iam_role.events.id
  policy = data.aws_iam_policy_document.events_policy.json
}

# --- Build: backend image + frontend, inside CodePipeline ---------------------------

resource "aws_codebuild_project" "builder" {
  name          = local.name
  description   = "Builds ${local.name}'s backend image and frontend in the customer account"
  service_role  = aws_iam_role.codebuild.arn
  build_timeout = 45

  artifacts {
    type = "CODEPIPELINE"
  }

  environment {
    compute_type    = "BUILD_GENERAL1_MEDIUM"
    image           = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    type            = "LINUX_CONTAINER"
    privileged_mode = true

    environment_variable {
      name  = "ECR_REGISTRY"
      value = local.registry
    }

    environment_variable {
      name  = "REPOSITORY_URI"
      value = aws_ecr_repository.app.repository_url
    }

    environment_variable {
      name  = "REPOSITORY_NAME"
      value = aws_ecr_repository.app.name
    }

    environment_variable {
      name  = "IMAGE_TAG"
      value = var.image_tag
    }

    environment_variable {
      name  = "ECS_CLUSTER"
      value = local.name
    }

    environment_variable {
      name  = "ECS_SERVICE"
      value = local.name
    }

    environment_variable {
      name  = "BACKEND_DIR"
      value = var.backend_dir
    }

    environment_variable {
      name  = "FRONTEND_ENABLED"
      value = tostring(local.frontend)
    }

    environment_variable {
      name  = "FRONTEND_DIR"
      value = var.frontend_dir
    }

    environment_variable {
      name  = "FRONTEND_BUILD"
      value = tostring(var.frontend_build)
    }

    environment_variable {
      name  = "FRONTEND_OUTPUT"
      value = var.frontend_output
    }

    environment_variable {
      name  = "WEB_BUCKET"
      value = local.frontend ? aws_s3_bucket.web[0].bucket : ""
    }

    environment_variable {
      name  = "DISTRIBUTION_COMMENT"
      value = local.name
    }

    # Build-time variables the frontend reads for its API base URL; the build sets
    # each to https://<distribution domain><suffix> once it has found the distribution.
    environment_variable {
      name  = "FRONTEND_API_ENV"
      value = join(" ", var.frontend_api_env)
    }

    environment_variable {
      name  = "FRONTEND_API_SUFFIX"
      value = var.frontend_api_suffix
    }
  }

  # Docker layers (base images, unchanged layers) and the source stay on the build
  # host between runs, so rebuilds skip most of the pull and install work.
  cache {
    type  = "LOCAL"
    modes = ["LOCAL_DOCKER_LAYER_CACHE", "LOCAL_SOURCE_CACHE", "LOCAL_CUSTOM_CACHE"]
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.codebuild.name
    }
  }

  source {
    type      = "CODEPIPELINE"
    buildspec = <<-BUILDSPEC
      version: 0.2
      env:
        shell: bash
      cache:
        paths:
          - '/root/.npm/**/*'
      phases:
        install:
          runtime-versions:
            nodejs: 20
        pre_build:
          on-failure: ABORT
          commands:
            - aws ecr get-login-password --region "$AWS_DEFAULT_REGION" | docker login --username AWS --password-stdin "$ECR_REGISTRY"
        build:
          on-failure: ABORT
          commands:
            - |
              set -euo pipefail
              if aws ecr describe-images --repository-name "$REPOSITORY_NAME" --image-ids imageTag="$IMAGE_TAG" >/dev/null 2>&1; then
                echo "Backend image $IMAGE_TAG already exists; skipping the backend build"
              else
                docker build -t "$REPOSITORY_URI:$IMAGE_TAG" "./$BACKEND_DIR"
                docker push "$REPOSITORY_URI:$IMAGE_TAG"
              fi
            - |
              set -euo pipefail
              if [ "$FRONTEND_ENABLED" = "true" ]; then
                # The distribution is created in parallel with this build; find it by its comment.
                DIST_ID=""
                DIST_DOMAIN=""
                for attempt in $(seq 1 60); do
                  read -r DIST_ID DIST_DOMAIN <<< "$(aws cloudfront list-distributions --query "DistributionList.Items[?Comment=='$DISTRIBUTION_COMMENT'] | [0].[Id,DomainName]" --output text 2>/dev/null || true)"
                  if [ -n "$DIST_ID" ] && [ "$DIST_ID" != "None" ]; then break; fi
                  echo "Waiting for the CloudFront distribution to exist..."
                  sleep 20
                done
                if [ -z "$DIST_ID" ] || [ "$DIST_ID" = "None" ]; then
                  echo "CloudFront distribution $DISTRIBUTION_COMMENT not found"
                  exit 1
                fi
                for v in $FRONTEND_API_ENV; do export "$v=https://$DIST_DOMAIN$FRONTEND_API_SUFFIX"; done
                cd "$CODEBUILD_SRC_DIR/$FRONTEND_DIR"
                if [ "$FRONTEND_BUILD" = "true" ]; then
                  if [ -f package-lock.json ]; then npm ci; else npm install; fi
                  npm run build
                fi
                OUT=""
                for d in $FRONTEND_OUTPUT dist build out; do
                  if [ -f "$d/index.html" ]; then OUT="$d"; break; fi
                done
                if [ -z "$OUT" ]; then
                  f="$(ls dist/*/browser/index.html dist/*/index.html 2>/dev/null | head -n 1 || true)"
                  if [ -n "$f" ]; then OUT="$(dirname "$f")"; fi
                fi
                if [ -z "$OUT" ]; then
                  echo "No built index.html found in $FRONTEND_DIR (looked in dist/, build/ and out/)"
                  exit 1
                fi
                aws s3 sync "$OUT" "s3://$WEB_BUCKET" --delete --only-show-errors
                aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/*" >/dev/null
                echo "Frontend from $FRONTEND_DIR/$OUT published"
              fi
        post_build:
          commands:
            - |
              if aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" --query 'services[?status==`ACTIVE`].serviceName' --output text 2>/dev/null | grep -q .; then
                aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" --force-new-deployment >/dev/null
                echo "Rolled $ECS_SERVICE onto $IMAGE_TAG"
              else
                echo "$ECS_SERVICE doesn't exist yet; it starts directly on $IMAGE_TAG when Terraform creates it"
              fi
    BUILDSPEC
  }

  depends_on = [aws_iam_role_policy.codebuild, aws_cloudwatch_log_group.codebuild]
}

# --- Pipeline: runs once when created, then on every new source.zip --------------

resource "aws_codepipeline" "app" {
  name          = local.name
  role_arn      = aws_iam_role.codepipeline.arn
  pipeline_type = "V2"

  artifact_store {
    location = aws_s3_bucket.source.bucket
    type     = "S3"
  }

  stage {
    name = "Source"

    action {
      name             = "Source"
      category         = "Source"
      owner            = "AWS"
      provider         = "S3"
      version          = "1"
      output_artifacts = ["source"]

      configuration = {
        S3Bucket             = aws_s3_bucket.source.bucket
        S3ObjectKey          = aws_s3_object.source.key
        PollForSourceChanges = "false"
      }
    }
  }

  stage {
    name = "Build"

    action {
      name            = "BuildAndDeploy"
      category        = "Build"
      owner           = "AWS"
      provider        = "CodeBuild"
      version         = "1"
      input_artifacts = ["source"]

      configuration = {
        ProjectName = aws_codebuild_project.builder.name
      }
    }
  }

  depends_on = [aws_iam_role_policy.codepipeline, aws_s3_object.source]
}

resource "aws_cloudwatch_event_rule" "source_updated" {
  name        = "${local.name}-src"
  description = "Start ${local.name}'s pipeline when a new source.zip is uploaded"

  event_pattern = jsonencode({
    source      = ["aws.s3"]
    detail-type = ["Object Created"]
    detail = {
      bucket = { name = [aws_s3_bucket.source.bucket] }
      object = { key = [aws_s3_object.source.key] }
    }
  })
}

resource "aws_cloudwatch_event_target" "source_updated" {
  rule     = aws_cloudwatch_event_rule.source_updated.name
  arn      = aws_codepipeline.app.arn
  role_arn = aws_iam_role.events.arn
}

# --- Security groups -----------------------------------------------------------

# With CloudFront, only CloudFront can reach the load balancer, so every request
# goes through the HTTPS URL. Without it (Dev preset, no separate frontend) the
# load balancer is the app's public HTTP endpoint.
resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = local.cdn ? "HTTP from CloudFront only" : "HTTP from the internet"
  vpc_id      = aws_vpc.main.id

  dynamic "ingress" {
    for_each = local.cdn ? [1] : []
    content {
      description     = "HTTP from CloudFront origin-facing servers"
      from_port       = 80
      to_port         = 80
      protocol        = "tcp"
      prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront.id]
    }
  }

  dynamic "ingress" {
    for_each = local.cdn ? [] : [1]
    content {
      description = "HTTP from the internet (no CloudFront)"
      from_port   = 80
      to_port     = 80
      protocol    = "tcp"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }

  egress {
    description = "To the containers"
    from_port   = var.container_port
    to_port     = var.container_port
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }
}

resource "aws_security_group" "task" {
  name        = "${local.name}-task"
  description = "Container port from the load balancer only"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "From the load balancer"
    from_port       = var.container_port
    to_port         = var.container_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    description = "Image pulls, secrets, logs, the database and outbound API calls"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "db" {
  count       = local.db_enabled ? 1 : 0
  name        = "${local.name}-db"
  description = "Database port from the application containers only"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "From the application containers"
    from_port       = local.db_port
    to_port         = local.db_port
    protocol        = "tcp"
    security_groups = [aws_security_group.task.id]
  }
}

# --- Load balancer (origin for CloudFront) ------------------------------------------

resource "aws_lb" "app" {
  name                       = local.name
  internal                   = false
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.alb.id]
  subnets                    = aws_subnet.public[*].id
  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "app" {
  name                 = local.name
  port                 = var.container_port
  protocol             = "HTTP"
  vpc_id               = aws_vpc.main.id
  target_type          = "ip"
  deregistration_delay = 10

  health_check {
    path                = var.health_check_path
    matcher             = "200-499"
    interval            = 10
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

# --- CloudFront: the app's one public URL ------------------------------------------

resource "aws_cloudfront_origin_access_control" "web" {
  count                             = local.frontend ? 1 : 0
  name                              = "${local.name}-web"
  description                       = "CloudFront access to ${local.name}'s frontend bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Single-page apps: paths without a file extension get index.html.
resource "aws_cloudfront_function" "spa" {
  count   = local.frontend ? 1 : 0
  name    = "${local.name}-spa"
  runtime = "cloudfront-js-2.0"
  comment = "Serve index.html for client-side routes"
  publish = true
  code    = <<-JS
    function handler(event) {
      var request = event.request;
      var last = request.uri.split('/').pop();
      if (last.indexOf('.') === -1) {
        request.uri = '/index.html';
      }
      return request;
    }
  JS
}

# Backends whose routes don't start with /api: strip the prefix CloudFront routes on.
resource "aws_cloudfront_function" "strip_api" {
  count   = local.frontend && var.api_strip_prefix ? 1 : 0
  name    = "${local.name}-api"
  runtime = "cloudfront-js-2.0"
  comment = "Remove the /api routing prefix before the backend"
  publish = true
  code    = <<-JS
    function handler(event) {
      var request = event.request;
      request.uri = request.uri.replace(/^\/api(?=\/|$)/, '') || '/';
      return request;
    }
  JS
}

resource "aws_cloudfront_distribution" "app" {
  count   = local.cdn ? 1 : 0
  enabled = true
  # Don't hold the apply for global propagation (often 5-15 min): the distribution
  # serves from the first edge locations within minutes, while the rest catch up.
  wait_for_deployment = false
  comment             = local.name
  price_class         = var.price_class
  is_ipv6_enabled     = true
  http_version        = "http2and3"
  default_root_object = local.frontend ? "index.html" : null

  origin {
    origin_id   = "api"
    domain_name = aws_lb.app.dns_name

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
      origin_read_timeout    = 60
    }
  }

  dynamic "origin" {
    for_each = local.frontend ? [1] : []
    content {
      origin_id                = "web"
      domain_name              = aws_s3_bucket.web[0].bucket_regional_domain_name
      origin_access_control_id = aws_cloudfront_origin_access_control.web[0].id
    }
  }

  default_cache_behavior {
    target_origin_id         = local.frontend ? "web" : "api"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = local.frontend ? ["GET", "HEAD", "OPTIONS"] : ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods           = ["GET", "HEAD"]
    compress                 = true
    cache_policy_id          = local.frontend ? data.aws_cloudfront_cache_policy.optimized.id : data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = local.frontend ? null : data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id

    dynamic "function_association" {
      for_each = local.frontend ? [1] : []
      content {
        event_type   = "viewer-request"
        function_arn = aws_cloudfront_function.spa[0].arn
      }
    }
  }

  dynamic "ordered_cache_behavior" {
    for_each = local.frontend ? [1] : []
    content {
      path_pattern             = "/api/*"
      target_origin_id         = "api"
      viewer_protocol_policy   = "redirect-to-https"
      allowed_methods          = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
      cached_methods           = ["GET", "HEAD"]
      compress                 = true
      cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
      origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id

      dynamic "function_association" {
        for_each = var.api_strip_prefix ? [1] : []
        content {
          event_type   = "viewer-request"
          function_arn = aws_cloudfront_function.strip_api[0].arn
        }
      }
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

# --- ECS -----------------------------------------------------------------------

resource "aws_ecs_cluster" "app" {
  name = local.name
}

resource "aws_ecs_task_definition" "app" {
  family                   = local.name
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = tostring(var.cpu)
  memory                   = tostring(var.memory_mb)
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name      = "app"
      image     = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
      essential = true
      portMappings = [
        {
          containerPort = var.container_port
          hostPort      = var.container_port
          protocol      = "tcp"
        }
      ]
      environment = local.container_environment
      secrets     = local.container_secrets
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.ecs.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "ecs"
        }
      }
    }
  ])

  depends_on = [aws_iam_role_policy.execution, aws_cloudwatch_log_group.ecs]
}

# The image builds in parallel with RDS and CloudFront, so it usually exists by
# the time this service is created. Tasks also need every secret to have a value;
# until then the circuit breaker stops the deployment, and a force-new-deployment
# (CodeBuild's, or the customer's after filling the secrets) starts a fresh one.
resource "aws_ecs_service" "app" {
  name                              = local.name
  cluster                           = aws_ecs_cluster.app.id
  task_definition                   = aws_ecs_task_definition.app.arn
  desired_count                     = var.desired_count
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = 30

  deployment_circuit_breaker {
    enable   = true
    rollback = false
  }

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "app"
    container_port   = var.container_port
  }

  depends_on = [aws_lb_listener.http, aws_iam_role_policy.execution]
}
