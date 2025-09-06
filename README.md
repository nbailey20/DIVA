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

DIVA runs periodically (via Eventbridge cronjob rule), iterating through all events:

1. **Detection**  
   - If detection succeeds → event is marked healthy.  
   - If detection fails → failure count increments in state.

2. **Injection**  
   - Only performed if needed (on first failure or if configured to reinject each period).  
   - Injection failures increment a separate counter.  

3. **Warmup Handling**  
  - Events can enter a configurable "warmup" period before failures are counted.  
  - During warmup, failures are tracked but not treated as alerts.  
  - Warmup completes after either a fixed number of successful detections.  

4. **Alerting**  
   - Triggered when 1 or more max thresholds are reached:  
     - `max_failed_detections` → alert for repeated detection failures  
     - `max_failed_injections` → alert for repeated injection failures  

5. **State persistence**  
   - Monolithic mode → persisted to **S3** JSON object.  
   - Distributed mode → persisted per-event to **DynamoDB** to allow concurrent access.  

6. **Recovery / Reset**  
   - If an event is successfully detected, state resets according to `reset_policy`:  
     - `"fast"` → reset failures and injection flags immediately  
     - `"cooldown"` → increment a `cooldown_counter` each successful detection until a threshold is reached, then reset  

> All other details of historical failures and injections are logged via Lambda in CloudWatch.

---

## Event State
Each event has a state object that tracks minimal but essential information:

| Field                  | Description                                                                 |
|------------------------|-----------------------------------------------------------------------------|
| `detection_failures`   | Consecutive failed detections.                                              |
| `injection_failures`   | Consecutive failed injections.                                              |
| `injected`             | Whether the event has been successfully injected.                           |
| `alerted`              | Whether an alert has been generated for this event.                         |
| `cooldown_counter`     | Counts consecutive successful detections after an alert in "cooldown" mode. |
| `in_warmup       `     | Whether the event is currently in warmup mode (failures not counted).       |
| `warmup_counter`       | Number of consecutive successful detections in warmup mode.                 |


- State is persisted to **S3** in monolithic mode and **DynamoDB** in distributed mode.
- On a successful detection, the state may be reset according to `reset` configuration:
  - `"fast"`: Immediately resets the state to defaults.
  - `"cooldown"`: Increments `cooldown_counter` and resets only after `success_threshold` consecutive successful detections.
- `warmup` can delay failure counting either for a fixed number of periods or until the first successful detection (whichever is configured).
- CloudWatch logging provides a complete chronological record of detections, injections, warmup transitions, and resets.

---

## Reset Policies

`reset` determines how the event state is updated after successful detection:

| Key       | Type    | Description                                                                                          |
|-----------|---------|------------------------------------------------------------------------------------------------------|
| `mode`    | string  | `"fast"` = immediate reset, `"cooldown"` = reset after threshold of successful detections.            |
| `success_threshold` | Integer | Number of consecutive successful detections required before cooldown resets.    |

- Default mode: `"fast"`.
- `success_threshold` (default: 3) specifies how many consecutive successful detections are required before state reset in cooldown mode.
- Alerts and failure counters are cleared when state is reset.

---

## Warmup

The `warmup` object delays failure counting until the system stabilizes:

| Key         | Type    | Description                                                                                     |
|-------------|---------|-------------------------------------------------------------------------------------------------|
| `enabled`   | bool | Whether warmup is enabled or not.                                 |
| `success_threshold` | int | Failures are ignored until this many successful detections are observed.        |

- Either or both options may be set.  
- Warmup ends once its conditions are satisfied, after which failures are tracked normally.  
- During warmup, events are reported as `"waiting (N×)"` instead of `"failed"`. 

Defaults:
- `enabled`: `False`
- `success_threshold`: `2` (if enabled)


---

## Module Variables

| Variable               | Type     | Description                                                                                                                        | Default         |
|------------------------|----------|------------------------------------------------------------------------------------------------------------------------------------|-----------------|
| `diva_mode`            | string   | DIVA mode: `"monolithic"` (S3 state, single Lambda) or `"distributed"` (DynamoDB, separate Lambdas)                                | `"monolithic"`  |
| `lambda_name`          | string   | Name of the DIVA Lambda function                                                                                                   | n/a (required)  |
| `lambda_role_arn`      | string   | IAM role ARN that the Lambda will assume                                                                                           | n/a (required)  |
| `schedule_expression`  | string   | EventBridge cron or rate expression to invoke DIVA                                                                                 | `"rate(5 minutes)"` |
| `event_logic_path`     | string   | Path to the `user_logic.py` file for this module invocation                                                                        | n/a (required)  |
| `kms_key_arn`          | string   | Optional KMS CMK ARN for S3 encryption                                                                                             | `null`          |
| `parallelize_events`   | bool     | Whether DIVA probes events in parallel or serially                                                                                 | `true`          |
| `max_workers`          | number   | Number of threads if parallelized                                                                                                  | `8`             |
| `lambda_memory_mb`     | number   | Memory size (MB) for the Lambda function                                                                                           | `128`           |
| `lambda_timeout_sec`   | number   | Timeout (seconds) for the Lambda function                                                                                          | `60`            |
| `lambda_log_level`     | string   | Log level for the Lambda (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`)                                                         | `"INFO"`        |
| `lambda_vpc_config`    | object   | Optional VPC configuration for Lambda: `subnet_ids` and `security_group_ids`                                                       | `null`          |
| `s3_bucket_name`       | string   | Optionally provide an existing S3 bucket name for DIVA state storage. If null and `diva_mode=monolithic`, module creates a bucket. | `null`          |
| `dynamodb_table_name`  | string   | Optionally provide an existing DynamoDB table name for DIVA state storage. If null and `diva_mode=distributed`, module creates a table. | `null`      |

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

# DIVA `event_logic.py` — Events Map Options

| Option                   | Type     | Required | Description                                                                 | Example | Default |
|--------------------------|----------|----------|-----------------------------------------------------------------------------|---------|---------|
| `detect`                 | Callable | ✅ Yes if role is 'detect' or 'both'   | Function that checks if the event has occurred. Should return `True/False`. | `detect` | N/A |
| `inject`                 | Callable | ✅ Yes if role is 'inject' or 'both'    | Function that injects/triggers the event into the system.                   | `inject` | Omitted |
| `alert`                  | Callable | ✅ Yes   | Callback invoked when detection is triggered.                               | `alert` | N/A |
| `reset.mode`             | String   | ❌ No    | Reset policy after successful detection.<br>• `"fast"` = immediate reset<br>• `"cooldown"` = reset after threshold of successful detections | `"cooldown"` | `"fast"` |
| `reset.success_threshold` | Integer | ❌ No | Number of consecutive successful detections required before cooldown resets. | `2` | `3` |
| `warmup.enabled`         | Boolean  | ❌ No    | Whether or not warmup is enabled for the event.                                | `True` | `False` |
| `warmup.success_threshold` | Integer | ❌ No | Number of consecutive detections required to exit warmup mode.        | `1` | `2` if warmup enabled |
| `role`                   | String   | ❌ No    | Defines how DIVA should treat an event.<br>• detect, inject, both  | `"detect"` | `"both"` |
| `max_failed_injections`  | Integer  | ❌ No    | Maximum number of failed injection attempts before stopping further attempts. | `3` | `1` |
| `max_failed_detections`  | Integer  | ❌ No    | Maximum number of failed detections before stopping further checks.         | `5` | `3` |
| `inject_each_period`     | Boolean  | ❌ No    | If `true`, injection will be attempted in every monitoring period instead of once. | `True` | `False` |

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
            "reset": {
                "mode": "cooldown",         # use cooldown reset policy
                "success_threshold": 2 # reset after 2 consecutive successful detections
            }
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

  diva_mode           = "monolithic"
  schedule_expression = "rate(5 minutes)"
  event_logic_path    = "./event_logic.py"

  lambda_role_arn    = aws_iam_role.diva_lambda.arn
  lambda_log_level   = "DEBUG"  # optional, verbose for troubleshooting
  lambda_timeout_sec = 300      # optional
  parallelize_events = true     # optional, default true
  max_workers        = 8        # optional, default 8 threads for parallelization

  lambda_vpc_config = {         # optional
    subnet_ids         = ["subnet-123456"]
    security_group_ids = ["sg-123456"]
  }

  kms_key_arn    = aws_kms_key.diva.arn  # optional
  s3_bucket_name = "test-diva-bucket"    # optional for monolithic mode, will use lambda name otherwise
}
```

### Distributed Mode with New DB for State

```hcl
module "diva_distributed" {
  source = "./diva"

  diva_mode           = "distributed"
  schedule_expression = "rate(5 minutes)"
  event_logic_path    = "./event_logic.py"
  lambda_role_arn     = aws_iam_role.diva_lambda.arn
}
```

### Distributed Mode Connecting to Existing DB

```hcl
module "diva_distributed" {
  source = "./diva"

  diva_mode           = "distributed"
  schedule_expression = "rate(5 minutes)"
  event_logic_path    = "./event_logic.py"
  lambda_role_arn     = aws_iam_role.diva_lambda.arn

  dynamodb_table_name = "test-diva-ddb-table" ## existing DB table name to connect to for sharing state
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
INFO  Event 'event_1' still in warmup (waiting 2/2)
INFO  Alerting for event 'event_1'
INFO  Event 'event_1' alerted
```
Successful events, skipped detections, injections, cooldowns, and alerts are all clearly logged.  

Debug logs provide full event state for troubleshooting.  


# DIVA
