# DIVA Lambda Engine

DIVA (Detect->Inject->Verify->Alert) is an AWS Lambda-based monitoring engine that validates whether **events** can occur in a system.  
If you can write code to perform an action and verify its result, DIVA can periodically probe this functionality and detect issues.

It works by first **detecting** whether expected events are already occurring in a system by executing your custom detection logic.  
If the desired events are not detected, your custom **injection** logic is executed to **verify** the event can occur.  
An **alert** is generated if the event still fails after injection.

Because the detection and injection logic is 100% customizable (must be executable from Lambda), this engine can support a wide variety of use cases.

---

## Execution Modes

1. Monolithic Mode (default):
   - Runs detection and/or injection in sequence, determined by the event's `role`.
   - Detection always executes first if `role` includes `"detect"` or `"both"`.
   - Injection may run if `role` includes `"inject"` or `"both"` **and** detection failed (configurable via `inject_each_period`).
   - Useful for single-process, "self-contained" environments.
   - Logging will indicate if an event is skipped due to its role.

2. Distributed Mode:
   - Each event is assigned a `role`: `"detect"` or `"inject"`.
   - Only the matching function runs, depending on the event's role.
   - Useful for scaling detection and injection across different workers.
   - Unified alerting can still trigger if either failure counter exceeds thresholds.

---

## How It Works

Each **event** to monitor is defined by you in `event_logic.py` and consists of:

- **detect** function – checks whether the event occurred. Returns `True` if healthy, `False` otherwise.  
- **inject** function – triggers the event in the target system. Returns `True` if injection was successful, `False` otherwise.  
- Optional **alert** function – invoked if an event fails detection or injection beyond allowed thresholds.  

These functions receive the event name as input to differentiate multiple events. They can be as simple as a single print statement,
or as complicated as you like. The only limitation is that they must be executable within Lambda.

DIVA runs periodically (via CloudWatch schedule), iterating through all events:

1. **Detection**  
   - If detection succeeds → event is marked healthy.  
   - If detection fails → failure count increments in state.

2. **Injection**  
   - Only performed if needed (on first failure or if configured to reinject each period).  
   - Injection failures increment a separate counter.  

3. **Alerting**  
   - Triggered once one or more max thresholds are reached:  
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

## Event State
Each event has a state object that tracks minimal but essential information:

| Field                  | Description                                                                 |
|------------------------|-----------------------------------------------------------------------------|
| `detection_failures`    | Consecutive failed detections.                                              |
| `injection_failures`    | Consecutive failed injections.                                              |
| `injected`              | Whether the event has been successfully injected.                          |
| `alerted`               | Whether an alert has been generated for this event.                        |
| `cooldown_counter`      | Counts consecutive successful detections after an alert in "cooldown" mode.|

- State is persisted to **S3** in monolithic mode and **DynamoDB** in distributed mode.
- On a successful detection, the state may be reset according to `reset_policy`:
  - `"fast"`: Immediately resets the state to defaults.
  - `"cooldown"`: Increments `cooldown_counter` and resets only after `cooldown_success_threshold` consecutive successful detections.
- CloudWatch logging provides a complete chronological record of detections and injections.

---

## Reset Policies

`reset_policy` determines how the event state is updated after successful detection:

| Policy    | Description                                                                                  |
|-----------|----------------------------------------------------------------------------------------------|
| `fast`    | Immediately resets the event state to default values.                                         |
| `cooldown`| Increments `cooldown_counter` on each successful detection. State is reset only after the counter reaches `cooldown_success_threshold`. |

- Default policy: `fast`.
- `cooldown_success_threshold` (default: 3) specifies how many consecutive successful detections are required before state reset in cooldown mode.
- Alerts and failure counters are cleared when state is reset.

---

## Module Variables

| Variable           | Type     | Description                                                                 | Default        |
|--------------------|----------|-----------------------------------------------------------------------------|----------------|
| `diva_mode`        | string   | `"monolithic"` (S3 state, single Lambda) or `"distributed"` (DynamoDB, separate Lambdas) | `"monolithic"` |
| `lambda_role_arn`  | string   | ARN of existing IAM role for the Lambda                                      | n/a (required) |
| `schedule`         | string   | CloudWatch EventBridge schedule expression for running DIVA                 | `"rate(5m)"`   |
| `vpc_config`       | object   | Optional VPC config: `subnet_ids` and `security_group_ids`                  | `null`         |
| `kms_key_arn`      | string   | Optional KMS key ARN to encrypt Lambda environment variables                | `null`         |
| `parallelize`  | bool   | Whether to process events in parallel.                                                       | `true`  |
| `max_workers`  | int    | Maximum threads used when parallelizing event processing.                                     | `8`     |
| `log_level`    | string | `"INFO"` or `"DEBUG"`. `INFO` = clean CloudWatch logs; `DEBUG` = verbose internal state.    | `"INFO"`|

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

You must provide this file alongside the Terraform module.

```python
def get_events():
    return {
        "event_1": {
            "detect": detect_sample,
            "inject": inject_sample,
            "alert": alert_sample,
            "role": "both",                 # perform detection + injection
            "max_failed_detections": 3,     # alert after 3 failed detections
            "max_failed_injections": 1,     # alert after 1 failed injection
            "inject_each_period": True,     # inject on every period if failed
            "reset_policy": "cooldown",     # cooldown reset after success
            "cooldown_success_threshold": 2 # reset after 2 consecutive successful detections
        },
        "event_2": {
            "inject": inject_sample,
            "alert": alert_sample,
            "role": "inject",               # only run injection
            "max_failed_injections": 2
        },
        "event_3": {
            "detect": detect_sample,
            "alert": alert_sample,
            "role": "detect",               # only run detection
            "max_failed_detections": 4
        },
    }

# --------------------
# Example detect function
# --------------------
def detect_sample(event_name):
    print(f"Checking event '{event_name}' in the target system")
    # Return True if healthy, False if event is missing
    return True

# --------------------
# Example inject function
# --------------------
def inject_sample(event_name):
    print(f"Injecting event '{event_name}' into the target system")
    # Return True if injection succeeded, False if failed
    return True

# --------------------
# Example alert function
# --------------------
def alert_sample(message):
    print(f"ALERT: {message}")
    # Integrate with Slack, SNS, PagerDuty, etc.
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

### Monolithic Mode (Default)

```hcl
module "diva_monolithic" {
  source = "./diva"

  diva_mode        = "monolithic"
  schedule         = "rate(5 minutes)"
  event_logic_path = "./event_logic.py"

  lambda_role_arn    = aws_iam_role.diva_lambda.arn
  lambda_log_level   = "DEBUG"  # verbose for troubleshooting
  lambda_timeout_sec = 300
  parallelize        = true
  max_workers        = 8
  

  lambda_vpc_config = {
    subnet_ids         = ["subnet-123456"]
    security_group_ids = ["sg-123456"]
  }

  kms_key_arn    = aws_kms_key.diva.arn
  s3_bucket_name = "test-diva-bucket"
}
```

### Distributed Mode with New DB for State

```hcl
module "diva_distributed" {
  source = "./diva"

  diva_mode           = "distributed"
  schedule            = "rate(5 minutes)"
  event_logic_path    = "./event_logic.py"
  lambda_role_arn     = aws_iam_role.diva_lambda.arn
}
```

### Distributed Mode Connecting to Existing DB

```hcl
module "diva_distributed" {
  source = "./diva"

  diva_mode        = "distributed"
  schedule         = "rate(5 minutes)"
  event_logic_path = "./event_logic.py"
  lambda_role_arn  = aws_iam_role.diva_lambda.arn

  dynamodb_table_name = "test-diva-ddb-table"
}
```

---

## Example Logs
```text
INFO  Getting events...  
INFO  Retrieved 4 events  
INFO  Detecting event 'event_1'...  
INFO  Detection for event 'event_1' failed (1/3)  
INFO  Injecting event 'event_1'...  
INFO  Detection for event 'event_2' skipped due to role='inject'  
INFO  Injecting event 'event_2'...  
INFO  Detection for event 'event_3' succeeded  
INFO  Event 'event_3' cooldown complete → state fully reset  
INFO  Alerting for event 'event_1'  
INFO  Event 'event_1' alerted  
```
Successful events, skipped detections, injections, cooldowns, and alerts are all clearly logged.  

Debug logs provide full event state for troubleshooting.  


# DIVA
