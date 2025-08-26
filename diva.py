import os
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.exceptions import ClientError

import event_logic  # user-defined event logic to be executed by DIVA


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

DEFAULT_EVENT_STATE = {
    "detection_failures": 0,
    "injected": False,
    "last_injection_ts": None,
    "first_detection_failure_ts": None,
    "last_detection_failure_ts": None,
    "alerted": False,
}


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
def load_state(event_id: str):
    if DIVA_MODE == "monolithic":
        try:
            obj = s3.get_object(Bucket=STATE_BUCKET, Key=STATE_KEY)
            return json.loads(obj["Body"].read().decode("utf-8"))
        except s3.exceptions.NoSuchKey:
            return {}
    else:
        table = dynamodb.Table(DDB_TABLE)
        try:
            resp = table.get_item(Key={"event_id": event_id})
            logging.debug("DynamoDB get_item response: %s", str(resp))
            return resp.get("Item", {})
        except ClientError:
            logging.error("Error reading DIVA state from DynamoDB", exc_info=True)
            return {}

def save_state(event_id: str, state: dict):
    ## merge with defaults to ensure all fields exist
    ## values from 'state' take precedence
    event_state = {**DEFAULT_EVENT_STATE, **state}

    if DIVA_MODE == "monolithic":
        # dump the whole state blob as a single object in S3
        try:
            s3.put_object(
                Bucket=STATE_BUCKET,
                Key=STATE_KEY,
                Body=json.dumps(state).encode("utf-8")
            )
        except Exception:
            logging.error("Error writing DIVA state to S3", exc_info=True)
    # distributed → DynamoDB
    else:
        table = dynamodb.Table(DDB_TABLE)
        try:
            table.put_item(
                Item={
                    "event_id": event_id,
                    **event_state
                }
            )
        except ClientError:
            logging.error("Error writing DIVA state to DynamoDB", exc_info=True)


def _load_event_state(event_name: str, full_state: dict | None = None) -> dict:
    """Load state for a single event, backend-agnostic."""
    if DIVA_MODE == "monolithic":
        return full_state.get(event_name, DEFAULT_EVENT_STATE.copy())
    else:  # distributed → DynamoDB
        state = load_state(event_name)
        return {**DEFAULT_EVENT_STATE, **state}


def _save_event_state(event_name: str, event_state: dict, full_state: dict | None = None):
    """Save state for a single event, backend-agnostic."""
    if DIVA_MODE == "monolithic":
        full_state[event_name] = event_state
    else:  # distributed → DynamoDB
        save_state(event_name, event_state)


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
        # Reset fields in state on successful detection
        event_state["detection_failures"] = 0
        event_state["first_detection_failure_ts"] = None
        event_state["last_detection_failure_ts"] = None
        event_state["alerted"] = False
        event_state["injected"] = False 
        event_state["last_injection_ts"] = None

        results[event_name] = "ok"
        return event_state, results

    ## if not detected, record failure timestamps
    event_state["detection_failures"] += 1
    if event_state["first_detection_failure_ts"] is None:
        event_state["first_detection_failure_ts"] = now
    event_state["last_detection_failure_ts"] = now

    ## if failures exceed threshold, send alert once
    if event_state["detection_failures"] >= funcs.get("max_failures", 1) and not event_state.get("alerted", False):
        msg = f"DIVA Alert: event '{event_name}' failed {event_state['detection_failures']} times (threshold={funcs.get('max_failures', 1)})"
        send_alert(funcs, msg)
        event_state["alerted"] = True

    results[event_name] = f"failures={event_state['detection_failures']}"
    return event_state, results

# --------------------
# INJECTION FUNCTION
# --------------------
def should_inject(event_state, funcs):
    """Decide if injection should run based on detection failures + config."""
    failures = event_state.get("detection_failures", 0)

    if failures == 0:
        return False, "skipped (healthy)"

    if failures == 1 or funcs.get("inject_each_period", False):
        return True, "injected"

    return False, f"waiting ({failures}x)"


def process_injection(event_name, funcs, event_state):
    results = {}
    now = int(time.time())

    do_inject, status = should_inject(event_state, funcs)
    if do_inject:
        funcs["inject"](event_name)
        event_state["injected"] = True
        event_state["last_injection_ts"] = now

    results[event_name] = status
    return event_state, results

# --------------------
# MONOLITHIC: DETECT + INJECT
# --------------------
def process_detection_and_injection(event_name, funcs, event_state):
    # Run detection
    event_state, results = process_detection(event_name, funcs, event_state)

    # Decide if we should inject (only if detection failed)
    do_inject, status = should_inject(event_state, funcs)
    if do_inject:
        event_state, inject_results = process_injection(event_name, funcs, event_state)
        results.update(inject_results)
    else:
        results[event_name] = status

    return event_state, results


# --------------------
# PROCESS SINGLE event
# --------------------
def process_event(event_name, funcs, event_state):
    role = funcs.get("role", "both")  # "both", "detect", "inject"

    try:
        if DIVA_MODE == "distributed":
            if role == "detect":
                return process_detection(event_name, funcs, event_state)
            elif role == "inject":
                return process_injection(event_name, funcs, event_state)
            else:
                logging.debug("Skipping event '%s' in distributed mode", event_name)
                return event_state, {event_name: "skipped"}

        # Monolithic mode: detection + injection
        return process_detection_and_injection(event_name, funcs, event_state)

    except Exception as e:
        msg = f"DIVA Exception in event '{event_name}': {e}"
        send_alert(funcs, msg)
        return event_state, {event_name: "error"}


# --------------------
# SERIAL event PROCESSING
# --------------------
def process_events_serial(events: dict, full_state: dict | None = None):
    results = {}

    for event_name, funcs in events.items():
        event_state = _load_event_state(event_name, full_state)
        _, event_result = process_event(event_name, funcs, event_state)
        _save_event_state(event_name, event_state, full_state)
        results.update(event_result)

    return results, full_state

# --------------------
# PARALLEL event PROCESSING
# --------------------
def process_events_parallel(events: dict, full_state: dict | None = None):
    results = {}

    def _process_single(event_name: str, funcs: dict):
        event_state = _load_event_state(event_name, full_state)
        event_state, event_result = process_event(event_name, funcs, event_state)
        _save_event_state(event_name, event_state, full_state)
        return event_result

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_process_single, name, funcs): name for name, funcs in events.items()}

        for fut in as_completed(futures):
            try:
                logging.debug("Completed event '%s'", fut.result())
                results.update(fut.result())
            except Exception as e:
                logging.error("Error processing event '%s': %s", futures[fut], e, exc_info=True)

    return results, full_state


# --------------------
# Main HANDLER
# --------------------
def lambda_handler(_, __):
    setup_logging()
    logging.debug("Starting DIVA Lambda")

    events = event_logic.get_events()
    logging.debug("Retrieved events %s", str(events))

    results = {}

    if DIVA_MODE == "monolithic":
        # load the whole state once for S3
        logging.debug("Loading full state from S3")
        state = load_state("*")  # returns dict of all events

        if PARALLELIZE:
            logging.debug("Parallelizing events")
            results, new_state = process_events_parallel(events, state)
        else:
            logging.debug("Serializing events")
            results, new_state = process_events_serial(events, state)

        logging.debug("Saving full state back to S3")
        save_state(None, new_state)  # event_id=None means full blob
        logging.debug("Saved state")

    else:  # distributed → DynamoDB per-event rows
        if PARALLELIZE:
            logging.debug("Parallelizing events for distributed mode")
            results, _ = process_events_parallel(events)
        else:
            logging.debug("Serializing events for distributed mode")
            results, _ = process_events_serial(events)

    logging.debug("Results %s", str(results))
    return results
