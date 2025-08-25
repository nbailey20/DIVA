# DIVA Lambda Engine

DIVA (Detect->Inject->Verify->Alert) is an AWS Lambda-based monitoring engine that validates whether events can occur in a system.
If you can write code to do something and also validate whether that thing happened, DIVA can periodically probe this functionality and detect issues.

It works by first **detecting** whether expected events are already occurring in a system by executing your custom detection logic. 
If the desired events are not detected, your custom **injection** logic is executed to **verify** the event can occur. 
An **alert** is generated if the event is still not detected after injection.

Because the injection and detection logic is 100% customizable with the only constraint being executability from a Lambda function, this engine can support a wide variety of applications.

The module supports two execution modes:

- **Monolithic mode** (default):  
  A single Lambda function runs both injection and detection logic. State is stored in **S3**.  
  ✅ Used where injection and detection can be executed from the same system.

- **Distributed mode**:  
  Separate Lambdas run injection and detection independently. State is stored in **DynamoDB**.  
  ✅ Used for cross-device or cross-region events, where injection and detection happen on different systems or in a hub/spoke architecture.

---

## How It Works

Each **event** to monitor is defined in `user_logic.py` and consists of:
- An **inject** function – generates the event in the target system.
- A **detect** function – checks whether the event successfully occurred.
- An optional **alert** function – notifies your monitoring/alerting system (e.g., SNS, PagerDuty, Slack).
These 3 functions all accept the event name as input to differentiate between different events.
They can be as complicated as you like, or as simple as a single print statement.

DIVA runs periodically (via a CloudWatch schedule), iterating through all expected events:
1. **Detection first**:  
   - If detection succeeds → event is marked healthy.
   - If detection fails → failures are tracked in state.
2. **Injection next** (when needed):  
   - On the first failure, or on every run if configured, an injection is attempted.
   - Multiple consecutive failures eventually trigger the **alert** function once max_failures is reached.
3. **State persistence**:  
   - In monolithic mode → persisted to **S3** JSON object.
   - In distributed mode → persisted to **DynamoDB**.

---

## Module Variables

| Variable           | Type     | Description                                                                 | Default        |
|--------------------|----------|-----------------------------------------------------------------------------|----------------|
| `diva_mode`        | string   | Engine mode: `"monolithic"` (S3 state, 1 Lambda) or `"distributed"` (DynamoDB state, 2 Lambdas). | `"monolithic"` |
| `lambda_role_arn`  | string   | ARN of the IAM role to attach to the Lambda. Must already exist.            | n/a (required) |
| `schedule`         | string   | CloudWatch EventBridge schedule expression for how often DIVA runs.         | `"rate(5m)"`   |
| `vpc_config`       | object   | Optional. Provide `subnet_ids` and `security_group_ids` to attach Lambda to a VPC. Example:<br>`{ subnet_ids = ["subnet-123"], security_group_ids = ["sg-123"] }` | `null` |
| `kms_key_arn`      | string   | Optional. ARN of KMS key to encrypt Lambda environment variables.           | `null`         |

---

## Module Outputs

| Output                    | Description |
|----------------------------|-------------|
| `lambda_function_name`     | Name of the DIVA Lambda function. |
| `lambda_function_arn`      | ARN of the DIVA Lambda function. |
| `state_bucket_name`        | (Monolithic mode only) Name of the S3 bucket storing DIVA state. |
| `dynamodb_table_name`      | (Distributed mode only) Name of the DynamoDB table storing DIVA state. |
| `eventbridge_rule_name`    | Name of the EventBridge rule that triggers the Lambda. |
| `eventbridge_rule_arn`     | ARN of the EventBridge rule that triggers the Lambda. |
| `lambda_execution_role_arn`| ARN of the IAM role attached to the Lambda (provided externally). |

---

## Example `user_logic.py`

You must provide this file alongside the Lambda package.

```python
def get_events():
    return {
        "sample_event": {
            "inject": inject_sample,
            "detect": detect_sample,
            "alert": alert_sample,   # optional
            "max_failures": 3,       # default: 1
            "inject_each_period": False
        }
    }

# Example inject function
def inject_sample(event_name):
    print(f"Injecting test data for {event_name}")
    # e.g., write a test message into SQS, Kafka, Pub/Sub, etc.

# Example detect function
def detect_sample(event_name):
    print(f"Checking data arrival for {event_name}")
    # e.g., query the destination system
    return True  # return True if healthy, False if not

# Example alert function
def alert_sample(message):
    print(f"ALERT: {message}")
    # e.g., send to Slack, SNS, PagerDuty, etc.
```
---

## IAM Permissions

This module **does not create IAM roles or policies**.  
You must attach the following permissions to the Lambda’s IAM role depending on the chosen `DIVA_MODE`.

### Common (all modes)
- `logs:CreateLogGroup`
- `logs:CreateLogStream`
- `logs:PutLogEvents`

### Monolithic Mode (`DIVA_MODE=monolithic`)
Uses **S3** to persist state.
- `s3:GetObject` (for the state file)
- `s3:PutObject` (to update the state file)
- If `var.kms_key_arn` is set:
  - `kms:Decrypt`
  - `kms:Encrypt`
  - `kms:GenerateDataKey`

### Distributed Mode (`DIVA_MODE=distributed`)
Uses **DynamoDB** to persist state.
- `dynamodb:GetItem`
- `dynamodb:PutItem`
- `dynamodb:UpdateItem`
- `dynamodb:DeleteItem`
- `dynamodb:DescribeTable`
- If `var.kms_key_arn` is set:
  - `kms:Decrypt`
  - `kms:Encrypt`
  - `kms:GenerateDataKey`

### Optional (if Lambda runs inside a VPC)
If you attach the Lambda to a VPC for private networking:
- `ec2:CreateNetworkInterface`
- `ec2:DescribeNetworkInterfaces`
- `ec2:DeleteNetworkInterface`

---

## Example Usage

```hcl
module "diva" {
  source = "./diva"

  lambda_role_arn = aws_iam_role.diva_lambda.arn
  diva_mode       = "monolithic" # or "distributed"
  schedule        = "rate(5 minutes)"

  vpc_config = {
    subnet_ids         = ["subnet-123456"]
    security_group_ids = ["sg-123456"]
  }

  kms_key_arn = aws_kms_key.diva.arn
}
```
# DIVA
