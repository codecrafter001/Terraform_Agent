output "url" {
  description = "HTTPS endpoint of the function."
  value       = aws_lambda_function_url.function.function_url
}

output "function_name" {
  value = aws_lambda_function.function.function_name
}

output "function_arn" {
  value = aws_lambda_function.function.arn
}

output "live_version" {
  value = aws_lambda_alias.live.function_version
}

