import os
import json
import logging
import importlib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.exceptions import ClientError


# --------------------
# CONFIGURATION
# --------------------
STATE_BUCKET  = os.environ.get("DIVA_STATE_BUCKET", "my-diva-state-bucket")
STATE_KEY     = os.environ.get("DIVA_STATE_KEY", "diva_state.json")
DDB_TABLE     = os.environ.get("DIVA_DDB_TABLE", "diva_state") ## either S3 or DynamoDB for state depending on diva_mode
PARALLELIZE   = bool(os.environ.get("DIVA_PARALLELIZE", "true"))
MAX_WORKERS   = int(os.environ.get("DIVA_MAX_WORKERS", 8))
DIVA_MODE     = os.environ.get("DIVA_MODE", "monolithic").lower()  # "monolithic" or "distributed"
LOG_LEVEL_STR = os.environ.get("DIVA_LOG_LEVEL", "INFO").upper()

s3         = boto3.client("s3")
dynamodb   = boto3.resource("dynamodb")
user_logic = importlib.import_module("user_logic")

# --------------------
# LOGGING
# --------------------
def setup_logging():
    log_level = getattr(logging, LOG_LEVEL_STR, logging.INFO)
    logging.getLogger().setLevel(log_level)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

# --------------------
# STATE BACKENDS
# --------------------
def get_state():
    if DIVA_MODE == "monolithic":
        try:
            resp = s3.get_object(Bucket=STATE_BUCKET, Key=STATE_KEY)
            return json.loads(resp["Body"].read())
        except s3.exceptions.NoSuchKey:
            return {}
        except Exception:
            logging.error("Error reading DIVA state file from S3", exc_info=True)
            return {}
    else:  # distributed → DynamoDB
        table = dynamodb.Table(DDB_TABLE)
        try:
            resp = table.get_item(Key={"state_id": "diva"})
            return resp.get("Item", {}).get("state", {})
        except ClientError:
            logging.error("Error reading DIVA state from DynamoDB", exc_info=True)
            return {}

def save_state(data):
    if DIVA_MODE == "monolithic":
        try:
            s3.put_object(
                Bucket=STATE_BUCKET,
                Key=STATE_KEY,
                Body=json.dumps(data).encode("utf-8")
            )
        except Exception:
            logging.error("Error writing DIVA state to S3", exc_info=True)
    else:  # distributed → DynamoDB
        table = dynamodb.Table(DDB_TABLE)
        try:
            table.put_item(Item={"state_id": "diva", "state": data})
        except ClientError:
            logging.error("Error writing DIVA state to DynamoDB", exc_info=True)

# --------------------
# ALERTING
# --------------------
def send_alert(funcs, message):
    if "alert" in funcs and callable(funcs["alert"]):
        funcs["alert"](message)
    else:
        logging.error(message)


# --------------------
# DETECTION FUNCTION
# --------------------
def process_detection(event_name, funcs, event_state):
    now = int(time.time())
    results = {}

    if funcs["detect"](event_name):
        results[event_name] = "ok"
        return event_state, results

    ## if not detected, record failure timestamps
    event_state["failures"] += 1
    if event_state["first_failure_ts"] is None:
        event_state["first_failure_ts"] = now
    event_state["last_failure_ts"] = now

    ## if failures exceed threshold, send alert once
    if event_state["failures"] >= funcs.get("max_failures", 1) and not event_state.get("alerted", False):
        msg = f"DIVA Alert: event '{event_name}' failed {event_state['failures']} times (threshold={funcs.get('max_failures', 1)})"
        send_alert(funcs, msg)
        event_state["alerted"] = True

    results[event_name] = f"failures={event_state['failures']}"
    return event_state, results

# --------------------
# INJECTION FUNCTION
# --------------------
def process_injection(event_name, funcs, event_state):
    funcs["inject"](event_name)
    event_state["injected"] = True
    event_state["last_injected_ts"] = int(time.time())
    results = {event_name: "injected"}
    return event_state, results

# --------------------
# MONOLITHIC: DETECT + INJECT
# --------------------
def process_detection_and_injection(event_name, funcs, event_state):
    # Call detection
    event_state, results = process_detection(event_name, funcs, event_state)

    # If detection failed, optionally inject
    if results[event_name].startswith("failures"):
        if event_state["failures"] == 1 or funcs.get("inject_each_period", False):
            event_state, inject_results = process_injection(event_name, funcs, event_state)
            results.update(inject_results)
        else:
            # just waiting
            results[event_name] = f"waiting ({event_state['failures']}x)"

    return event_state, results


# --------------------
# PROCESS SINGLE event
# --------------------
def process_event(event_name, funcs, event_state):
    now = int(time.time())
    results = {}

    # initialize state fields
    event_state.setdefault("failures", 0)
    event_state.setdefault("injected", False)
    event_state.setdefault("last_injected_ts", None)
    event_state.setdefault("first_failure_ts", None)
    event_state.setdefault("last_failure_ts", None)
    event_state.setdefault("alerted", False)

    max_failures = funcs.get("max_failures", 1)
    role = funcs.get("role", "both")  # "both", "detect", "inject"

    try:
        # Skip events that don't match role in distributed mode
        if DIVA_MODE == "distributed":
            if role == "detect":
                return event_name, process_detection(event_name, funcs, event_state)
            elif role == "inject":
                return event_name, process_injection(event_name, funcs, event_state)
            else:
                logging.debug("Skipping event '%s' in distributed mode", event_name)
                return event_name, event_state, {event_name: "skipped"}

        # Monolithic mode: call detection then injection
        return event_name, process_detection_and_injection(event_name, funcs, event_state)

    except Exception as e:
        msg = f"DIVA Exception in event '{event_name}': {e}"
        send_alert(funcs, msg)
        results[event_name] = "error"
        return event_name, event_state, results


# --------------------
# SERIAL event PROCESSING
# --------------------
def process_events_serial(events, state):
    results = {}
    new_state = {}

    for event_name, funcs in events.items():
        event_state, event_result = process_event(event_name, funcs, state.get(event_name, {}))
        if event_state is not None:
            new_state[event_name] = event_state
        results.update(event_result)

    return results, new_state

# --------------------
# PARALLEL event PROCESSING
# --------------------
def process_events_parallel(events, state):
    results = {}
    new_state = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(process_event, event_name, funcs, state.get(event_name, {})): event_name
            for event_name, funcs in events.items()
        }

        for fut in as_completed(futures):
            event_name, event_state, event_result = fut.result()
            if event_state is not None:
                new_state[event_name] = event_state
            results.update(event_result)

    return results, new_state


# --------------------
# Main HANDLER
# --------------------
def lambda_handler(event, context):
    setup_logging()

    logging.debug("Starting DIVA Lambda")
    logging.debug("About to get state from S3")
    state = get_state()
    logging.debug("Retrieved state %s", str(state))
    logging.debug("About to get events from user logic")
    events = user_logic.get_events()
    logging.debug("Retrieved events %s", str(events))
    results = {}

    if PARALLELIZE:
        logging.debug("Parallelizing events")
        results, new_state = process_events_parallel(events, state)
    else:
        logging.debug("Serializing events")
        results, new_state = process_events_serial(events, state)

    logging.debug("Results %s", str(results))
    logging.debug("New state %s", str(new_state))
    logging.debug("About to save state to S3")
    save_state(new_state)
    logging.debug("Saved state")
    return results
