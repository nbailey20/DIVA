module "diva_detect" {
  source           = "../diva"

  diva_mode           = "monolithic"
  event_logic_path    = "./event_logic.py"
  schedule_expression = "rate(1 minute)"
  parallelize_events  = true
  kms_key_arn         = null

  lambda_name        = "diva-test-mono-lambda"
  lambda_role_arn    = "" ## role with lambda:InvokeFunction, s3:GetObject, s3:PutObject, logs:CreateLogGroup, logs:CreateLogStream, logs:PutLogEvents
  lambda_timeout_sec = 300
  # lambda_log_level   = "DEBUG"
}
