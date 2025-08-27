"""
========================================
 DIVA Engine - Design Overview
========================================

DIVA (Detect->Inject->Verify->Alert) is an AWS Lambda-based monitoring engine that validates whether events can occur in a system. Each event has:
  - A detection function (verifies expected signals/conditions)
  - An injection function (creates conditions that should trigger detection)
  - Configurable thresholds and reset behavior

The engine is responsible for:
  1. Running detections periodically.
  2. Deciding when to inject events.
  3. Tracking failures and triggering alerts.
  4. Resetting event state after recovery.

----------------------------------------
 Execution Modes
----------------------------------------

1. Monolithic Mode (default):
   - Each event declares a role: "detect", "inject", or "both" (default).
   - If role = "detect": only detection runs.
   - If role = "inject": only injection runs.
   - If role = "both" (default): detection runs first, and injection may run
     if detection fails (configurable).
   - Useful for single-process, "self-contained" environments where both
     sides of validation can be coordinated together.

2. Distributed Mode:
   - Each event is assigned a role: "detect" or "inject".
   - Only the matching function runs, depending on the event's role.
   - Useful for scaling detection and injection across different workers.

----------------------------------------
 Unified Alerting
----------------------------------------

- Alerts are *per-event*, not per-role.
- An event may trigger alerts if either detection or injection
  failures exceed their respective thresholds.
- This design avoids duplicate alerts and ensures state resets apply
  consistently across both detection and injection logic.

----------------------------------------
 State Management
----------------------------------------

Each event maintains a state dictionary:

    {
        "detection_failures": 0,
        "injection_failures": 0,
        "alerted": False,
        "cooldown_counter": 0,
        "injected": False,
    }

This state is persisted between runs and updated after each cycle.

Reset Policy (after successful detection):
  - "fast": Reset immediately to DEFAULT_EVENT_STATE.
  - "cooldown": Increment `cooldown_counter`; reset only after N
    consecutive successes.

----------------------------------------
 Customization
----------------------------------------

Each event can override defaults with function parameters:
  - max_failed_detections
  - max_failed_injections
  - reset_policy
  - cooldown_success_threshold
  - inject_each_period
  - role (detect/inject/both)

Defaults are defined in module-level constants.

----------------------------------------
 Error Handling
----------------------------------------

- If a detection or injection function raises an exception,
  the engine catches it, logs it, and sends an alert.
- State continues from the last known values (not reset).
"""

import os
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.exceptions import ClientError

import event_logic  # user-defined event logic to be executed by DIVA


# --------------------
# CONFIGURATION
# --------------------
STATE_BUCKET  = os.environ.get("DIVA_STATE_BUCKET", "my-diva-state-bucket")
STATE_KEY     = os.environ.get("DIVA_STATE_KEY", "diva_state.json")
DDB_TABLE     = os.environ.get("DIVA_DDB_TABLE", "diva_state")  # Either S3 or DynamoDB for state depending on diva_mode
PARALLELIZE   = bool(os.environ.get("DIVA_PARALLELIZE", "true"))
MAX_WORKERS   = int(os.environ.get("DIVA_MAX_WORKERS", 8))
DIVA_MODE     = os.environ.get("DIVA_MODE", "monolithic").lower()  # "monolithic" or "distributed"
LOG_LEVEL_STR = os.environ.get("DIVA_LOG_LEVEL", "INFO").upper()

s3         = boto3.client("s3")
dynamodb   = boto3.resource("dynamodb")


# --------------------
# DEFAULTS
# --------------------
DEFAULT_EVENT_STATE = {
    "detection_failures": 0,   # Consecutive failed detections
    "injection_failures": 0,   # Consecutive failed injections
    "injected": False,         # Whether the event has been successfully injected
    "alerted": False,          # Whether an alert has been generated for the event
    "cooldown_counter": 0,     # Counts successful detections during cooldown period after alert
}

DEFAULT_MAX_FAILED_DETECTIONS = 3
DEFAULT_MAX_FAILED_INJECTIONS = 1
DEFAULT_INJECT_EACH_PERIOD    = False
DEFAULT_RESET_POLICY          = "fast"      # "fast" = reset immediately, "cooldown" = reset after N successes
DEFAULT_COOLDOWN_THRESHOLD    = 3
DEFAULT_DIVA_ROLE             = "both"      # "both", "detect", "inject"


# --------------------
# LOGGING
# --------------------
def setup_logging():
    """Configure root logger with DIVA’s log level and suppress noisy dependencies."""
    LOG_FORMAT = "[%(levelname)s] %(message)s"
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    log_level = getattr(logging, LOG_LEVEL_STR, logging.INFO)
    logging.getLogger().setLevel(log_level)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# --------------------
# STATE BACKENDS
# --------------------
def load_state(event_id: str):
    """
    Load persisted state from backend.

    Monolithic mode → entire state blob from S3.
    Distributed mode → per-event row from DynamoDB.
    """
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
    """
    Persist event state to backend.

    Monolithic mode → write whole state blob to S3.
    Distributed mode → upsert per-event row in DynamoDB.
    """
    # Merge with defaults to guarantee all fields exist
    event_state = {**DEFAULT_EVENT_STATE, **state}

    if DIVA_MODE == "monolithic":
        try:
            s3.put_object(
                Bucket=STATE_BUCKET,
                Key=STATE_KEY,
                Body=json.dumps(state).encode("utf-8")
            )
        except Exception:
            logging.error("Error writing DIVA state to S3", exc_info=True)
    else:
        table = dynamodb.Table(DDB_TABLE)
        try:
            table.put_item(Item={"event_id": event_id, **event_state})
        except ClientError:
            logging.error("Error writing DIVA state to DynamoDB", exc_info=True)


def _load_event_state(event_name: str, full_state: dict | None = None) -> dict:
    """Helper: load state for a single event, backend-agnostic."""
    if DIVA_MODE == "monolithic":
        return full_state.get(event_name, DEFAULT_EVENT_STATE.copy())
    else:
        state = load_state(event_name)
        return {**DEFAULT_EVENT_STATE, **state}


def _save_event_state(event_name: str, event_state: dict, full_state: dict | None = None):
    """Helper: save state for a single event, backend-agnostic."""
    if DIVA_MODE == "monolithic":
        full_state[event_name] = event_state
    else:
        save_state(event_name, event_state)


def _reset_event_state(event_state, reset_policy: str, cooldown_threshold: int):
    """
    Reset or update event state depending on reset policy after a successful detection.

    Args:
        event_state (dict): Current state of the event.
        reset_policy (str): "fast" = immediately reset all counters,
                            "cooldown" = reset after N consecutive successes.
        cooldown_threshold (int): Number of consecutive successful detections required in cooldown mode.

    Returns:
        dict: Updated event state.
    """
    if reset_policy == "fast":
        if event_state.get("alerted"):
            logging.info("Resetting event alert")
        return DEFAULT_EVENT_STATE.copy()
    elif reset_policy == "cooldown":
        # Only increment cooldown counter if the event was previously alerted
        if event_state.get("alerted", False):
            event_state["cooldown_counter"] = event_state.get("cooldown_counter", 0) + 1
            if event_state["cooldown_counter"] >= cooldown_threshold:
                return DEFAULT_EVENT_STATE.copy()
    return event_state


# --------------------
# ALERTING
# --------------------
def send_alert(funcs, message):
    """Send alert using event-specific alerting function if provided, else log as error."""
    if "alert" in funcs and callable(funcs["alert"]):
        funcs["alert"](message)
    else:
        logging.error(message)


# --------------------
# DETECTION
# --------------------
def process_detection(event_name, funcs, event_state):
    """
    Run detection logic for an event and update state.

    - Increments detection failure counter on failures.
    - Resets counters on success, based on reset_policy.
    - Triggers alert when max_failed_detections is exceeded.
    """
    results = {}

    max_failed_detections = funcs.get("max_failed_detections", DEFAULT_MAX_FAILED_DETECTIONS)
    reset_policy = funcs.get("reset_policy", DEFAULT_RESET_POLICY)
    cooldown_threshold = funcs.get("cooldown_success_threshold", DEFAULT_COOLDOWN_THRESHOLD)

    logging.info("Detecting event '%s'...", event_name)
    logging.debug("Detection start | event=%s | state=%s", event_name, event_state)

    try:
        detected = funcs["detect"](event_name)
    except Exception as e:
        event_state["detection_failures"] += 1
        logging.error("Detection exception in event '%s': %s", event_name, e, exc_info=True)
        logging.debug("Detection error | event=%s | state=%s", event_name, event_state)
        results[event_name] = f"detection_error ({event_state['detection_failures']}x/{max_failed_detections})"
        return event_state, results

    if detected:
        logging.info("Detection for event '%s' succeeded", event_name)
        # Reset state according to policy
        old_state = event_state.copy()

        # Only track cooldown if event was alerted and using cooldown reset
        in_cooldown = event_state.get("alerted", False) and reset_policy == "cooldown"

        event_state = _reset_event_state(event_state, reset_policy, cooldown_threshold)

        if reset_policy == "fast":
            logging.debug("Fast reset applied | before=%s | after=%s", old_state, event_state)
        elif reset_policy == "cooldown" and in_cooldown:
            cc = event_state.get("cooldown_counter", 0)
            if cc < cooldown_threshold:
                logging.info(
                    "Event '%s' in cooldown (%d/%d)",
                    event_name, cc, cooldown_threshold
                )
            else:
                logging.info("Event '%s' cooldown complete → state fully reset", event_name)
            logging.debug("Cooldown reset applied | before=%s | after=%s", old_state, event_state)

        results[event_name] = "ok"
        return event_state, results

    # Detection failed → increment counter
    event_state["detection_failures"] += 1
    failures = event_state["detection_failures"]

    if failures >= max_failed_detections:
        logging.warning(
            "Detection for event '%s' failed (%d/%d) — threshold reached",
            event_name, failures, max_failed_detections
        )
    else:
        logging.info(
            "Detection for event '%s' failed (%d/%d)",
            event_name, failures, max_failed_detections
        )

    logging.debug("Detection fail | event=%s | state=%s", event_name, event_state)

    results[event_name] = f"failed_detection ({failures}/{max_failed_detections})"
    return event_state, results



# --------------------
# INJECTION
# --------------------
def should_inject(event_state, funcs):
    """
    Decide if injection should run based on detection failures and config.

    Returns:
        (bool, str): Whether to inject, and status string for reporting.
    """
    failures = event_state.get("detection_failures", 0)

    if failures == 0:
        return False, "skipped (healthy)"

    if failures == 1 or funcs.get("inject_each_period", DEFAULT_INJECT_EACH_PERIOD):
        return True, "injected"

    return False, f"waiting ({failures}x)"


def process_injection(event_name, funcs, event_state):
    """
    Run injection logic for an event and update state.

    - Executes funcs["inject"] if injection should occur.
    - Tracks injection failures.
    - Returns event state + status string.
    """
    results = {}

    do_inject, status = should_inject(event_state, funcs)
    if do_inject:
        logging.info("Injecting event '%s'...", event_name)
        logging.debug("Injection start | event=%s | state=%s", event_name, event_state)

        try:
            success = funcs["inject"](event_name)
        except Exception as e:
            logging.error("Injection exception in event '%s': %s", event_name, e, exc_info=True)
            event_state["injection_failures"] = event_state.get("injection_failures", 0) + 1
            results[event_name] = f"injection_error ({event_state['injection_failures']}x)"
            return event_state, results

        event_state["injected"] = success

        if success:
            logging.info("Injection for event '%s' succeeded", event_name)
            logging.debug("Injection success | event=%s | state=%s", event_name, event_state)
            status = "injection_ok"
        else:
            event_state["injection_failures"] = event_state.get("injection_failures", 0) + 1
            logging.warning(
                "Injection for event '%s' failed (%d/%d)",
                event_name,
                event_state["injection_failures"],
                funcs.get("max_failed_injections", DEFAULT_MAX_FAILED_INJECTIONS),
            )
            logging.debug("Injection fail | event=%s | state=%s", event_name, event_state)
            status = f"failed_injection ({event_state['injection_failures']}x)"
    else:
        logging.debug("Skipping injection for event '%s' (reason=%s)", event_name, status)

    results[event_name] = status
    return event_state, results


# --------------------
# MONOLITHIC (detect + conditional inject)
# --------------------
def process_detection_and_injection(event_name, funcs, event_state):
    """
    Run detection and/or injection depending on configured role.
    Used only in monolithic mode.
    """
    role = funcs.get("role", DEFAULT_DIVA_ROLE)
    results = {}

    if role in ("detect", "both"):
        event_state, detect_results = process_detection(event_name, funcs, event_state)
        results.update(detect_results)
    else:
        logging.info("Skipping detection for event '%s' due to role='%s'", event_name, role)

    if role in ("inject", "both"):
        event_state, inject_results = process_injection(event_name, funcs, event_state)
        results.update(inject_results)
    else:
        logging.info("Skipping injection for event '%s' due to role='%s'", event_name, role)

    return event_state, results


# --------------------
# EVENT WRAPPER
# --------------------
def process_event(event_name, funcs, event_state):
    """
    Process a single event in either monolithic or distributed mode.

    - Distributed → runs only detect or inject depending on role.
    - Monolithic → runs detect, then optional inject.
    - After processing, unified alert check is applied.
    """
    role = funcs.get("role", DEFAULT_DIVA_ROLE)

    try:
        if DIVA_MODE == "distributed":
            if role == "detect":
                event_state, results = process_detection(event_name, funcs, event_state)
            elif role == "inject":
                event_state, results = process_injection(event_name, funcs, event_state)
            else:
                logging.info("Skipping event '%s' (role=%s not applicable in distributed)", event_name, role)
                return event_state, {event_name: "skipped"}
        else:
            event_state, results = process_detection_and_injection(event_name, funcs, event_state)

        # Unified alert check (applies to both modes)
        max_failed_detections = funcs.get("max_failed_detections", DEFAULT_MAX_FAILED_DETECTIONS)
        max_failed_injections = funcs.get("max_failed_injections", DEFAULT_MAX_FAILED_INJECTIONS)

        if (
            event_state.get("detection_failures", 0) >= max_failed_detections or
            event_state.get("injection_failures", 0) >= max_failed_injections
        ) and not event_state.get("alerted", False):
            logging.info("Alerting for event '%s'", event_name)
            msg = (
                f"DIVA Alert: event '{event_name}' failures → "
                f"detection: {event_state.get('detection_failures', 0)}, "
                f"injection: {event_state.get('injection_failures', 0)}"
            )
            send_alert(funcs, msg)
            event_state["alerted"] = True
            logging.info("Event '%s' alerted", event_name)

        return event_state, results

    except Exception as e:
        logging.error("Error processing event '%s': %s", event_name, e, exc_info=True)
        send_alert(funcs, f"DIVA Exception in event '{event_name}': {e}")
        return event_state, {event_name: "error"}


# --------------------
# SERIAL PROCESSING
# --------------------
def process_events_serial(events: dict, full_state: dict | None = None):
    """Process all events sequentially."""
    results = {}

    for event_name, funcs in events.items():
        event_state = _load_event_state(event_name, full_state)
        _, event_result = process_event(event_name, funcs, event_state)
        _save_event_state(event_name, event_state, full_state)
        results.update(event_result)

    return results, full_state


# --------------------
# PARALLEL PROCESSING
# --------------------
def process_events_parallel(events: dict, full_state: dict | None = None):
    """Process all events in parallel using ThreadPoolExecutor."""
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
# MAIN LAMBDA HANDLER
# --------------------
def lambda_handler(_, __):
    """
    AWS Lambda entrypoint.

    - Retrieves user-defined events.
    - Loads state from backend (S3 or DynamoDB).
    - Processes events serially or in parallel depending on config.
    - Persists updated state.
    """
    setup_logging()
    logging.debug("Starting DIVA Lambda")

    logging.info("Getting events...")
    events = event_logic.get_events()
    logging.info("Retrieved %d events", len(events))

    results = {}

    if DIVA_MODE == "monolithic":
        logging.debug("Loading full state from S3")
        state = load_state("*")  # Returns dict of all events
        logging.info("Loaded state for %d events", len(state))

        if PARALLELIZE:
            logging.debug("Parallelizing events")
            results, new_state = process_events_parallel(events, state)
        else:
            logging.debug("Serializing events")
            results, new_state = process_events_serial(events, state)

        logging.debug("Saving full state to S3, state=%s", new_state)
        save_state(None, new_state)
        logging.info("Saved event state for %d events", len(new_state))

    else:  # distributed mode → DynamoDB
        if PARALLELIZE:
            logging.debug("Parallelizing events for distributed mode")
            results, _ = process_events_parallel(events)
        else:
            logging.debug("Serializing events for distributed mode")
            results, _ = process_events_serial(events)

    logging.info("Results %s", str(results))
    logging.info("DIVA Lambda complete")
    return results
