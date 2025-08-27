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


#--------------------
# DEFAULTS
#--------------------
DEFAULT_EVENT_STATE = {
    "detection_failures": 0, # consecutive failed detections
    "injection_failures": 0, # consecutive failed injections
    "injected": False, # whether the event has been successfully injected
    "alerted": False, # whether an alert has been generated for the event
    "cooldown_counter": 0, # counts successful detections during cooldown period after alert
}

DEFAULT_MAX_FAILED_DETECTIONS = 3
DEFAULT_MAX_FAILED_INJECTIONS = 1
DEFAULT_INJECT_EACH_PERIOD    = False
DEFAULT_RESET_POLICY          = "fast" # "fast", "cooldown"
DEFAULT_COOLDOWN_THRESHOLD    = 3
DEFAULT_DIVA_ROLE             = "both"  # "both", "detect", "inject"


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
            state = obj["Body"].read().decode("utf-8")
            logging.debug("Loaded state from S3: %s", state)
            return json.loads(state)
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


def _reset_event_state(event_state, reset_policy: str, cooldown_threshold: int):
    """
    Reset or update event state depending on reset policy after a successful detection.

    Args:
        event_state (dict): Current state of the event.
        reset_policy (str): 'fast' to immediately reset, 'cooldown' to reset after consecutive successful detections.
        cooldown_threshold (int): Number of consecutive successful detections before reset in 'cooldown' mode.

    Returns:
        dict: Updated event state.
    """
    if reset_policy == "fast":
        return DEFAULT_EVENT_STATE.copy()
    elif reset_policy == "cooldown":
        event_state["cooldown_counter"] = event_state.get("cooldown_counter", 0) + 1
        if event_state["cooldown_counter"] >= cooldown_threshold:
            return DEFAULT_EVENT_STATE.copy()

    return event_state


# --------------------
# ALERTING FUNCTION
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
    """
    Perform detection logic and update state.
    Handles max_failed_detections and reset policy after successful detection.
    """
    results = {}

    max_failed_detections = funcs.get("max_failed_detections", DEFAULT_MAX_FAILED_DETECTIONS)
    reset_policy = funcs.get("reset_policy", DEFAULT_RESET_POLICY)
    cooldown_threshold = funcs.get("cooldown_success_threshold", DEFAULT_COOLDOWN_THRESHOLD)

    # Run user detection
    detected = funcs["detect"](event_name)
    
    if detected:
        # Successful detection → handle reset
        event_state = _reset_event_state(event_state, reset_policy, cooldown_threshold)
        results[event_name] = "ok"
        return event_state, results

    # Detection failed → increment counter
    event_state["detection_failures"] += 1

    # Alert if threshold exceeded
    if event_state["detection_failures"] >= max_failed_detections and not event_state.get("alerted", False):
        msg = f"DIVA Alert: event '{event_name}' failed {event_state['detection_failures']} times (threshold={max_failed_detections})"
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

    if failures == 1 or funcs.get("inject_each_period", DEFAULT_INJECT_EACH_PERIOD):
        return True, "injected"

    return False, f"waiting ({failures}x)"


def process_injection(event_name, funcs, event_state):
    """
    Process injection for a single event.

    Assumes funcs["inject"](event_name) returns True/False for success.
    Tracks injection failures and raises alert if max_failed_injections exceeded.
    """
    results = {}

    do_inject, status = should_inject(event_state, funcs)
    if do_inject:
        success = funcs["inject"](event_name)
        event_state["injected"] = success

        if not success:
            event_state["injection_failures"] = event_state.get("injection_failures", 0) + 1
            status = f"failed_injection ({event_state['injection_failures']}x)"

    results[event_name] = status
    return event_state, results

# --------------------
# MONOLITHIC: DETECT + Conditional INJECT
# --------------------
def process_detection_and_injection(event_name, funcs, event_state):
    """
    Run detection and/or injection depending on event role.
    Monolithic mode: allows conditional execution based on role.
    """
    role = funcs.get("role", DEFAULT_DIVA_ROLE)
    results = {}

    # Run detection if role includes "detect" or "both"
    if role in ("detect", "both"):
        event_state, detect_results = process_detection(event_name, funcs, event_state)
        results.update(detect_results)

    # Decide if we should inject if role includes "inject" or "both"
    if role in ("inject", "both"):
        event_state, inject_results = process_injection(event_name, funcs, event_state)
        results.update(inject_results)

    return event_state, results


# --------------------
# PROCESS SINGLE event
# --------------------
def process_event(event_name, funcs, event_state):
    """
    Process a single event.

    - Distributed mode: runs only the role-specified function (detect or inject).
    - Monolithic mode: detection + conditional injection.
    - Unified alert triggered if either failure counter exceeds its threshold.
    """
    role = funcs.get("role", DEFAULT_DIVA_ROLE)

    try:
        # --- Distributed mode ---
        if DIVA_MODE == "distributed":
            if role == "detect":
                event_state, results = process_detection(event_name, funcs, event_state)
            elif role == "inject":
                event_state, results = process_injection(event_name, funcs, event_state)
            else:
                logging.debug("Skipping event '%s' in distributed mode", event_name)
                return event_state, {event_name: "skipped"}

        # --- Monolithic mode ---
        else:
            event_state, results = process_detection_and_injection(event_name, funcs, event_state)

        # --- Unified alerting ---
        max_failed_detections = funcs.get("max_failed_detections", DEFAULT_MAX_FAILED_DETECTIONS)
        max_failed_injections = funcs.get("max_failed_injections", DEFAULT_MAX_FAILED_INJECTIONS)

        if (
            event_state.get("detection_failures", 0) >= max_failed_detections or
            event_state.get("injection_failures", 0) >= max_failed_injections
        ):
            if not event_state.get("alerted", False):
                msg = (
                    f"DIVA Alert: event '{event_name}' failures → "
                    f"detection: {event_state.get('detection_failures', 0)}, "
                    f"injection: {event_state.get('injection_failures', 0)}"
                )
                send_alert(funcs, msg)
                event_state["alerted"] = True

        return event_state, results

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
