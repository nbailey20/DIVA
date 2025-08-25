output "lambda_function_name" {
  description = "Name of the DIVA Lambda function"
  value       = aws_lambda_function.diva.function_name
}

output "lambda_function_arn" {
  description = "ARN of the DIVA Lambda function"
  value       = aws_lambda_function.diva.arn
}

output "state_bucket_name" {
  description = "Name of the S3 bucket storing state (monolithic mode only)"
  value       = var.diva_mode == "monolithic" ? aws_s3_bucket.diva_state[0].bucket : null
}

output "dynamodb_table_name" {
  description = "Name of the DynamoDB state table (distributed mode only)"
  value       = var.diva_mode == "distributed" ? aws_dynamodb_table.diva_state[0].name : null
}

output "eventbridge_rule_name" {
  description = "Name of the EventBridge rule that triggers the Lambda"
  value       = aws_cloudwatch_event_rule.diva_schedule.name
}

output "eventbridge_rule_arn" {
  description = "ARN of the EventBridge rule that triggers the Lambda"
  value       = aws_cloudwatch_event_rule.diva_schedule.arn
}

output "lambda_execution_role_arn" {
  description = "ARN of the IAM execution role attached to the Lambda (provided externally)"
  value       = var.lambda_role_arn
}
