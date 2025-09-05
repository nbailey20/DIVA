data "aws_caller_identity" "current" {}

# --------------------
# Package Lambda code
# --------------------
data "archive_file" "diva_lambda" {
  type        = "zip"
  output_path = "${path.module}/build/${var.lambda_name}.zip"

  source {
    content  = file("${path.module}/diva.py")
    filename = "diva.py"
  }

  source {
    content  = file(var.event_logic_path)
    filename = "event_logic.py"
  }
}
