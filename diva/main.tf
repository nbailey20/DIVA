resource "random_id" "s3_suffix" {
  byte_length = 4
}


# ---------------------------
# S3 for monolithic mode
# ---------------------------
resource "aws_s3_bucket" "diva_state" {
  count = var.diva_mode == "monolithic" ? 1 : 0

  bucket = var.s3_bucket_name != null ? var.s3_bucket_name : "${var.lambda_name}-state-${random_id.s3_suffix.hex}"

  tags = {
    Name = "diva-state"
    Mode = "monolithic"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "example" {
  count = var.diva_mode == "monolithic" ? 1 : 0

  bucket = aws_s3_bucket.diva_state[0].id

  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = var.kms_key_arn
      sse_algorithm     = var.kms_key_arn != null ? "aws:kms" : "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "diva_state" {
  count = var.diva_mode == "monolithic" ? 1 : 0

  bucket = aws_s3_bucket.diva_state[0].id

  versioning_configuration {
    status = "Enabled"
  }
}


# ---------------------------
# DynamoDB for distributed mode
# ---------------------------
resource "aws_dynamodb_table" "diva_state" {
  count = var.diva_mode == "distributed" && var.dynamodb_table_name == null ? 1 : 0

  name         = "${var.lambda_name}-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "event_id"

  attribute {
    name = "event_id"
    type = "S"
  }

  tags = {
    Name = "diva_state"
    Mode = "distributed"
  }
}

locals {
  state_table_name = (
    var.diva_mode == "distributed" ?
    (var.dynamodb_table_name != null ? var.dynamodb_table_name : aws_dynamodb_table.diva_state[0].name) :
    ""
  )
}

# --------------------
# Lambda function
# --------------------
resource "aws_lambda_function" "diva" {
  function_name = "${var.lambda_name}-${var.diva_mode}"
  role          = var.lambda_role_arn
  runtime       = "python3.12"
  handler       = "diva.lambda_handler"
  filename      = data.archive_file.diva_lambda.output_path
  memory_size   = var.lambda_memory_mb
  timeout       = var.lambda_timeout_sec
  kms_key_arn   = var.kms_key_arn != null ? var.kms_key_arn : null

  environment {
    variables = {
      DIVA_MODE         = var.diva_mode
      DIVA_STATE_BUCKET = var.diva_mode == "monolithic" ? aws_s3_bucket.diva_state[0].bucket : null
      DIVA_STATE_KEY    = "diva_state.json"
      DIVA_DDB_TABLE    = local.state_table_name
      DIVA_PARALLELIZE  = tostring(var.parallelize_events)
      DIVA_MAX_WORKERS  = tostring(var.max_workers)
      DIVA_LOG_LEVEL    = var.lambda_log_level
    }
  }

  dynamic "vpc_config" {
    for_each = var.lambda_vpc_config != null ? [var.lambda_vpc_config] : []
    content {
      subnet_ids         = vpc_config.value.subnet_ids
      security_group_ids = vpc_config.value.security_group_ids
    }
  }
}

# --------------------
# EventBridge rule + Lambda permission
# --------------------
resource "aws_cloudwatch_event_rule" "diva_schedule" {
  name                = "${var.lambda_name}-schedule"
  schedule_expression = var.schedule_expression
}

resource "aws_cloudwatch_event_target" "diva_lambda_target" {
  rule      = aws_cloudwatch_event_rule.diva_schedule.name
  target_id = "DivaLambda"
  arn       = aws_lambda_function.diva.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.diva.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.diva_schedule.arn
}