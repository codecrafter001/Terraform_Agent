# Function: a Lambda function behind an HTTPS function URL, with its own
# least-privilege execution role (logs only) under the /terraagent/ IAM path.

locals {
  name = "terraagent-${var.deployment_id}"
}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "function" {
  name                 = "${local.name}-fn"
  path                 = "/terraagent/"
  assume_role_policy   = data.aws_iam_policy_document.assume.json
  permissions_boundary = var.permissions_boundary_arn
}

resource "aws_cloudwatch_log_group" "function" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "logs" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.function.arn}:*"]
  }
}

resource "aws_iam_role_policy" "logs" {
  name   = "logs"
  role   = aws_iam_role.function.id
  policy = data.aws_iam_policy_document.logs.json
}

resource "aws_lambda_function" "function" {
  function_name    = local.name
  role             = aws_iam_role.function.arn
  runtime          = var.runtime
  handler          = var.handler
  filename         = "${path.module}/${var.package_file}"
  source_code_hash = var.package_sha256_b64
  memory_size      = var.memory_mb
  timeout          = var.timeout_s
  architectures    = ["x86_64"]
  publish          = true

  tracing_config {
    mode = "PassThrough"
  }

  depends_on = [aws_iam_role_policy.logs, aws_cloudwatch_log_group.function]
}

resource "aws_lambda_alias" "live" {
  name             = "live"
  description      = "Active deployment alias"
  function_name    = aws_lambda_function.function.function_name
  function_version = aws_lambda_function.function.version
}

resource "aws_lambda_function_url" "function" {
  function_name      = aws_lambda_function.function.function_name
  qualifier          = aws_lambda_alias.live.name
  authorization_type = var.public_url ? "NONE" : "AWS_IAM"
}

# A function URL with authorization NONE still needs a resource-based policy
# allowing public invocation through the URL.
resource "aws_lambda_permission" "public_url" {
  count = var.public_url ? 1 : 0

  statement_id           = "FunctionURLAllowPublicAccess"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.function.function_name
  qualifier              = aws_lambda_alias.live.name
  principal              = "*"
  function_url_auth_type = "NONE"
}

