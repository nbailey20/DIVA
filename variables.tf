variable "diva_mode" {
  description = "DIVA mode: monolithic (S3) or distributed (DynamoDB)"
  type        = string
  default     = "monolithic"
}

variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "lambda_name" {
  type        = string
  description = "Name of the DIVA Lambda function"
}

variable "schedule_expression" {
  type        = string
  description = "EventBridge cron or rate expression to invoke DIVA"
  default     = "rate(5 minutes)"
}

variable "event_logic_path" {
  description = "Path to the user_logic.py file for this module invocation"
  type        = string
}

variable "kms_key_arn" {
  type        = string
  description = "Optional KMS CMK ARN for S3 encryption"
  default     = null
}

variable "parallelize_events" {
  type        = bool
  description = "Whether DIVA probes events in parallel or serially"
  default     = true
}

variable "max_workers" {
  type        = number
  description = "Number of threads if parallelized"
  default     = 8
}

variable "lambda_memory_mb" {
  type    = number
  default = 128
}

variable "lambda_timeout_sec" {
  type    = number
  default = 60
}

variable "lambda_log_level" {
  type        = string
  description = "Log level for the Lambda (DEBUG, INFO, WARNING, ERROR, CRITICAL)"
  default     = "INFO"
}

variable "lambda_role_arn" {
  type        = string
  description = "IAM role ARN that the Lambda will assume"
}

variable "lambda_vpc_config" {
  type = object({
    subnet_ids         = list(string)
    security_group_ids = list(string)
  })
  description = "Optional VPC configuration for Lambda"
  default     = null
}

variable "s3_bucket_name" {
  description = "Optionally provide an existing S3 bucket name for DIVA state storage. If null and diva_mode=monolithic, the module will create its own bucket."
  type        = string
  default     = null
}

variable "dynamodb_table_name" {
  description = <<EOT
Optionally provide an existing DynamoDB table name for DIVA state storage.
This is used to connect multiple DIVA Lambda instances in distributed mode.
If null and diva_mode=distributed, the module will create its own table.
EOT
  type        = string
  default     = null
}
