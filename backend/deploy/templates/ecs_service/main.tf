# Container: an ECS Fargate service behind an Application Load Balancer, in its
# own VPC. The image is built inside the customer's account: Terraform uploads
# source.zip to S3, which starts a CodePipeline whose CodeBuild step builds and
# pushes the image to ECR and rolls the service. One approved apply does it all;
# TerraAgent never calls a build or deploy API itself.

locals {
  name     = "terraagent-${var.deployment_id}"
  azs      = slice(data.aws_availability_zones.available.names, 0, 2)
  boundary = coalesce(var.permissions_boundary_arn, "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:policy/TerraAgentWorkloadBoundary")
  registry = split("/", aws_ecr_repository.app.repository_url)[0]
}

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

# --- Network: dedicated VPC, two public subnets (no NAT gateway) --------------

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
    resources = [aws_ecs_service.app.id]
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

# --- Build: CodeBuild (inside CodePipeline) --------------------------------------

resource "aws_codebuild_project" "builder" {
  name          = local.name
  description   = "Builds the container image for ${local.name} in the customer account"
  service_role  = aws_iam_role.codebuild.arn
  build_timeout = 30

  artifacts {
    type = "CODEPIPELINE"
  }

  environment {
    compute_type    = "BUILD_GENERAL1_SMALL"
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
      value = aws_ecs_cluster.app.name
    }

    environment_variable {
      name  = "ECS_SERVICE"
      value = aws_ecs_service.app.name
    }
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
      phases:
        pre_build:
          on-failure: ABORT
          commands:
            - aws ecr get-login-password --region "$AWS_DEFAULT_REGION" | docker login --username AWS --password-stdin "$ECR_REGISTRY"
        build:
          on-failure: ABORT
          commands:
            - |
              if aws ecr describe-images --repository-name "$REPOSITORY_NAME" --image-ids imageTag="$IMAGE_TAG" >/dev/null 2>&1; then
                echo "Image $IMAGE_TAG already exists; skipping the build"
              else
                docker build -t "$REPOSITORY_URI:$IMAGE_TAG" .
                docker push "$REPOSITORY_URI:$IMAGE_TAG"
              fi
        post_build:
          commands:
            - aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" --force-new-deployment >/dev/null
            - echo "Rolled $ECS_SERVICE onto $IMAGE_TAG"
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

  depends_on = [aws_iam_role_policy.codepipeline, aws_s3_object.source, aws_ecs_service.app]
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

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "HTTP and HTTPS from the internet to the load balancer"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTP"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
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
    description = "Image pulls, logs and outbound API calls"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# --- Load balancer ---------------------------------------------------------------

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
  deregistration_delay = 30

  health_check {
    path                = var.health_check_path
    matcher             = "200-499"
    interval            = 30
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
    type             = var.certificate_arn != null ? "redirect" : "forward"
    target_group_arn = var.certificate_arn != null ? null : aws_lb_target_group.app.arn

    dynamic "redirect" {
      for_each = var.certificate_arn != null ? [1] : []
      content {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
}

resource "aws_lb_listener" "https" {
  count             = var.certificate_arn != null ? 1 : 0
  load_balancer_arn = aws_lb.app.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
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
      environment = [
        { name = "PORT", value = tostring(var.container_port) },
        { name = "HOST", value = "0.0.0.0" },
        { name = "NODE_ENV", value = "production" },
      ]
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

# Until the pipeline has pushed the first image the tasks can't start; the
# circuit breaker stops that first deployment, and CodeBuild's force-new-deployment
# starts a fresh one as soon as the image exists.
resource "aws_ecs_service" "app" {
  name                              = local.name
  cluster                           = aws_ecs_cluster.app.id
  task_definition                   = aws_ecs_task_definition.app.arn
  desired_count                     = var.desired_count
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = 60

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
