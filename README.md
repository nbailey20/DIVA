# DIVA Lambda Engine

DIVA (Detect->Inject->Verify->Alert) is an AWS Lambda-based monitoring engine that validates whether **events** can occur in a system.  
If you can write code to perform an action and verify its result, DIVA can periodically probe this functionality and detect issues.

It works by first **detecting** whether expected events are already occurring in a system by executing your custom detection logic.  
If the desired events are not detected, your custom **injection** logic is executed to **verify** the event can occur.  
An **alert** is generated if the event still fails after injection.

Because the detection and injection logic is 100% customizable (must be executable from Lambda), this engine can support a wide variety of use cases.

---

## Execution Modes

- **Monolithic mode** (default):  
  - Single Lambda runs detection and injection logic.  
  - State is persisted in **S3**.  
  - ✅ Suitable when detection and injection can run from the same environment.

- **Distributed mode**:  
  - Separate Lambdas handle detection vs injection independently.  
  - State is persisted in **DynamoDB**.  
  - ✅ Suitable for cross-device or cross-region events where injection and detection occur on different systems.

---

## How It Works

Each **event** to monitor is defined in `event_logic.py` and consists of:

- **detect** function – checks whether the event occurred. Returns `True` if healthy, `False` otherwise.  
- **inject** function – triggers the event in the target system. Returns `True` if injection was successful, `False` otherwise.  
- Optional **alert** function – invoked if an event fails detection or injection beyond allowed thresholds.  

These functions receive the event name to differentiate multiple events.

DIVA runs periodically (via CloudWatch schedule), iterating through all events:

1. **Detection**  
   - If detection succeeds → event is marked healthy.  
   - If detection fails → failure count increments in state.

2. **Injection**  
   - Only performed if needed (on first failure or if configured to reinject each period).  
   - Injection failures increment a separate counter.  

3. **Alerting**  
   - Triggered once max thresholds are reached:  
     - `max_failed_detections` → alert for repeated detection failures  
     - `max_failed_injections` → alert for repeated injection failures  

4. **State persistence**  
   - Monolithic mode → persisted to **S3** JSON object.  
   - Distributed mode → persisted per-event to **DynamoDB**.  

5. **Recovery / Reset**  
   - If an event is successfully detected, state may reset according to `reset_policy`:  
     - `"fast"` → reset failures and injection flags immediately  
     - `"cooldown"` → increment a `cooldown_counter` each successful detection until a threshold is reached, then reset  

> All other details of historical failures and injections are logged via Lambda in CloudWatch.

---

## Module Variables

| Variable           | Type     | Description                                                                 | Default        |
|--------------------|----------|-----------------------------------------------------------------------------|----------------|
| `diva_mode`        | string   | `"monolithic"` (S3 state, single Lambda) or `"distributed"` (DynamoDB, separate Lambdas) | `"monolithic"` |
| `lambda_role_arn`  | string   | ARN of existing IAM role for the Lambda                                      | n/a (required) |
| `schedule`         | string   | CloudWatch EventBridge schedule expression for running DIVA                 | `"rate(5m)"`   |
| `vpc_config`       | object   | Optional VPC config: `subnet_ids` and `security_group_ids`                  | `null`         |
| `kms_key_arn`      | string   | Optional KMS key ARN to encrypt Lambda environment variables                | `null`         |

---

## Module Outputs

| Output                    | Description |
|----------------------------|-------------|
| `lambda_function_name`     | Name of the DIVA Lambda function. |
| `lambda_function_arn`      | ARN of the DIVA Lambda function. |
| `state_bucket_name`        | (Monolithic mode only) S3 bucket storing DIVA state. |
| `dynamodb_table_name`      | (Distributed mode only) DynamoDB table storing per-event state. |
| `eventbridge_rule_name`    | Name of the EventBridge rule triggering the Lambda. |
| `eventbridge_rule_arn`     | ARN of the EventBridge rule triggering the Lambda. |
| `lambda_execution_role_arn`| ARN of the attached IAM role (provided externally). |

---

## Example `event_logic.py`

```python
def get_events():
    return {
        "test_event1": {
            "detect": detect_sample,
            "inject": inject_sample,
            "alert": alert_sample,
            "max_failed_detections": 3,
            "max_failed_injections": 2,
            "inject_each_period": True,
            "reset_policy": "cooldown",
            "cooldown_success_threshold": 2,
        },
        "test_event2": {
            "role": "detect",
            "detect": detect_sample,
            "alert": alert_sample,
        },
    }

def detect_sample(event_name):
    print(f"Checking event: {event_name}")
    return True  # True if detected

def inject_sample(event_name):
    print(f"Injecting event: {event_name}")
    return True  # True if injection succeeded

def alert_sample(message):
    print(f"ALERT: {message}")
```
---

## IAM Permissions

This module **does not create IAM roles or policies**. Attach these permissions to the Lambda role:

### Common (all modes)
- `logs:CreateLogGroup`
- `logs:CreateLogStream`
- `logs:PutLogEvents`

### Monolithic Mode (`DIVA_MODE=monolithic`)
- `s3:GetObject`
- `s3:PutObject`

### Distributed Mode (`DIVA_MODE=distributed`)
- `dynamodb:GetItem`
- `dynamodb:PutItem`
- `dynamodb:UpdateItem`
- `dynamodb:DeleteItem`
- `dynamodb:DescribeTable`

### Optional
- If attaching the Lambda to a VPC:
  - `ec2:CreateNetworkInterface`
  - `ec2:DescribeNetworkInterfaces`
  - `ec2:DeleteNetworkInterface`  
- If specifying a KMS CMEK via `kms_key_arn`:
  - `kms:Decrypt`
  - `kms:Encrypt`
  - `kms:GenerateDataKey`

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
