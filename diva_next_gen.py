"""
DIVA (Detect → Inject → Verify → Alert) - Refactored Engine

Key changes:
- State machine-style flow inside DivaEvent (detect → transitions → optional inject).
- Typed configs (WarmupConfig, ResetConfig) and Role enum for clarity.
- Outcome enum (SUCCESS/FAILURE/ERROR/SKIPPED) decouples logic from messaging.
- Pluggable state backends (S3 full-blob for monolithic, DynamoDB per-event for distributed).
- Clean orchestration with parallel/serial execution.
"""
from __future__ import annotations
import os
import json
import logging
from dataclasses import dataclass, asdict
from enum import Enum, auto
from typing import Any, Dict, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.exceptions import ClientError

import event_logic  # user-defined: must provide get_events() -> Dict[str, dict]


# =============================================================================
# Configuration & Defaults
# =============================================================================

STATE_BUCKET  = os.environ.get("DIVA_STATE_BUCKET", "my-diva-state-bucket")
STATE_KEY     = os.environ.get("DIVA_STATE_KEY", "diva_state.json")
DDB_TABLE     = os.environ.get("DIVA_DDB_TABLE", "diva_state")
PARALLELIZE   = os.environ.get("DIVA_PARALLELIZE", "true").lower() == "true"
MAX_WORKERS   = int(os.environ.get("DIVA_MAX_WORKERS", 8))
DIVA_MODE     = os.environ.get("DIVA_MODE", "monolithic").lower()  # "monolithic" | "distributed"
LOG_LEVEL_STR = os.environ.get("DIVA_LOG_LEVEL", "INFO").upper()

DEFAULT_DIVA_ROLE = "both"  # "both" | "detect" | "inject"

DEFAULT_MAX_FAILED_DETECTIONS = 3
DEFAULT_MAX_FAILED_INJECTIONS = 1
DEFAULT_INJECT_EACH_PERIOD    = False

# --- typed configs ------------------------------------------------------------

class Role(str, Enum):
    BOTH = "both"
    DETECT = "detect"
    INJECT = "inject"

@dataclass
class WarmupConfig:
    enabled: bool = False
    success_threshold: int = 2

@dataclass
class ResetConfig:
    mode: str = "fast"                # "fast" | "cooldown"
    on_verify: bool = True            # reset only after a successful detection
    cooldown_threshold: int = 3       # consecutive successes required to reset when in cooldown

# --- event state --------------------------------------------------------------

@dataclass
class EventState:
    detection_failures: int = 0
    injection_failures: int = 0
    injected: bool = False
    alerted: bool = False
    in_warmup: bool = False
    warmup_counter: int = 0
    cooldown_counter: int = 0

    @classmethod
    def from_dict(cls, d: Dict[str, Any], warmup_enabled: bool) -> EventState:
        base = cls()
        base.__dict__.update({**asdict(base), **(d or {})})
        # ensure warmup flag is aligned on first creation
        if d is None or d == {}:
            base.in_warmup = warmup_enabled
        return base

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

# --- outcomes ----------------------------------------------------------------

class Outcome(Enum):
    SUCCESS = auto()
    FAILURE = auto()
    ERROR   = auto()
    SKIPPED = auto()


# =============================================================================
# Logging
# =============================================================================

def setup_logging():
    fmt = "[%(levelname)s] %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt)
    logging.getLogger().setLevel(getattr(logging, LOG_LEVEL_STR, logging.INFO))
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# =============================================================================
# State Backends
# =============================================================================

class StateBackend:
    def load_all(self) -> Dict[str, Dict[str, Any]]:
        raise NotImplementedError

    def save_all(self, state: Dict[str, Dict[str, Any]]) -> None:
        raise NotImplementedError

    def load_one(self, event_id: str) -> Dict[str, Any]:
        raise NotImplementedError

    def save_one(self, event_id: str, state: Dict[str, Any]) -> None:
        raise NotImplementedError


class S3Backend(StateBackend):
    def __init__(self, bucket: str, key: str):
        self.s3 = boto3.client("s3")
        self.bucket = bucket
        self.key = key

    def load_all(self) -> Dict[str, Dict[str, Any]]:
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=self.key)
            blob = obj["Body"].read().decode("utf-8")
            return json.loads(blob)
        except self.s3.exceptions.NoSuchKey:
            return {}
        except Exception:
            logging.error("Failed to load state from S3", exc_info=True)
            return {}

    def save_all(self, state: Dict[str, Dict[str, Any]]) -> None:
        try:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=self.key,
                Body=json.dumps(state).encode("utf-8"),
            )
        except Exception:
            logging.error("Failed to save state to S3", exc_info=True)

    # monolithic uses full-blob; but provide no-ops for interface completeness
    def load_one(self, event_id: str) -> Dict[str, Any]:
        all_state = self.load_all()
        return all_state.get(event_id, {})

    def save_one(self, event_id: str, state: Dict[str, Any]) -> None:
        all_state = self.load_all()
        all_state[event_id] = state
        self.save_all(all_state)


class DynamoBackend(StateBackend):
    def __init__(self, table_name: str):
        self.table = boto3.resource("dynamodb").Table(table_name)

    def load_all(self) -> Dict[str, Dict[str, Any]]:
        # Only used in monolithic mode; Dynamo is per-event in this engine.
        raise NotImplementedError

    def save_all(self, state: Dict[str, Dict[str, Any]]) -> None:
        raise NotImplementedError

    def load_one(self, event_id: str) -> Dict[str, Any]:
        try:
            resp = self.table.get_item(Key={"event_id": event_id})
            return resp.get("Item", {})
        except ClientError:
            logging.error("DynamoDB get_item failed", exc_info=True)
            return {}

    def save_one(self, event_id: str, state: Dict[str, Any]) -> None:
        try:
            self.table.put_item(Item={"event_id": event_id, **state})
        except ClientError:
            logging.error("DynamoDB put_item failed", exc_info=True)


# =============================================================================
# Event Logic Wrapper (state machine-ish)
# =============================================================================

class DivaEvent:
    def __init__(self, name: str, funcs: Dict[str, Any], state: EventState):
        self.name = name
        self.funcs = funcs
        self.state = state

        # config normalization
        warmup_cfg_raw = funcs.get("warmup", {})
        reset_cfg_raw  = funcs.get("reset", {})

        self.warmup = WarmupConfig(
            enabled = bool(warmup_cfg_raw.get("enabled", False)),
            success_threshold = int(warmup_cfg_raw.get("success_threshold", 2)),
        )

        self.reset = ResetConfig(
            mode = reset_cfg_raw.get("mode", "fast"),
            on_verify = bool(reset_cfg_raw.get("on_verify", True)),
            cooldown_threshold = int(reset_cfg_raw.get("cooldown_threshold", reset_cfg_raw.get("cooldown_success_threshold", 3))),
        )

        self.role = Role(funcs.get("role", DEFAULT_DIVA_ROLE))

        self.max_failed_detections = int(funcs.get("max_failed_detections", DEFAULT_MAX_FAILED_DETECTIONS))
        self.max_failed_injections = int(funcs.get("max_failed_injections", DEFAULT_MAX_FAILED_INJECTIONS))
        self.inject_each_period    = bool(funcs.get("inject_each_period", DEFAULT_INJECT_EACH_PERIOD))

    # ---------------- Detection ----------------

    def detect(self) -> Outcome:
        if self.role == Role.INJECT:
            logging.info("Skipping detection for event '%s' due to role='inject'", self.name)
            return Outcome.SKIPPED

        logging.info("Detecting event '%s'...", self.name)
        logging.debug("Detection start | event=%s | state=%s", self.name, self.state.to_dict())

        try:
            ok = self.funcs["detect"](self.name)
        except Exception as e:
            if self.state.in_warmup:
                logging.error("Detection exception in event '%s' during warmup: %s", self.name, e, exc_info=True)
                return Outcome.ERROR
            self.state.detection_failures += 1
            logging.error("Detection exception in event '%s': %s", self.name, e, exc_info=True)
            return Outcome.ERROR

        if ok:
            return Outcome.SUCCESS

        # Failure path
        if self.state.in_warmup:
            # in warmup we don't count against thresholds; just reset warmup counter
            logging.info("Detection for event '%s' failed in warmup", self.name)
            self.state.warmup_counter = 0
            return Outcome.FAILURE

        self.state.detection_failures += 1
        failures = self.state.detection_failures
        if failures >= self.max_failed_detections:
            logging.warning(
                "Detection for event '%s' failed (%d/%d) — threshold reached",
                self.name, failures, self.max_failed_detections
            )
        else:
            logging.info(
                "Detection for event '%s' failed (%d/%d)",
                self.name, failures, self.max_failed_detections
            )
        logging.debug("Detection fail | event=%s | state=%s", self.name, self.state.to_dict())
        return Outcome.FAILURE

    # ---------------- Injection ----------------

    @staticmethod
    def _should_inject(state: EventState, inject_each_period: bool) -> Tuple[bool, str]:
        # Warmup: on first iteration (counter==0) we inject to prime the system.
        if state.in_warmup and state.warmup_counter == 0:
            return True, "injecting (warmup)"

        failures = state.detection_failures
        if failures == 0 and not inject_each_period:
            return False, "skipped (healthy)"

        # If we just discovered a failure, run at least one injection (unless we already injected)
        if (failures == 1 and not state.injected) or inject_each_period:
            return True, "injected"

        return False, f"waiting ({failures}x)"


    def inject(self) -> Tuple[Outcome, str]:
        if self.role == Role.DETECT:
            logging.info("Skipping injection for event '%s' due to role='detect'", self.name)
            return Outcome.SKIPPED, "skipped (role=detect)"

        do_inject, status = self._should_inject(self.state, self.inject_each_period)
        if not do_inject:
            logging.debug("Skipping injection for event '%s' (reason=%s)", self.name, status)
            return Outcome.SKIPPED, status

        logging.info("Injecting event '%s'...", self.name)
        logging.debug("Injection start | event=%s | state=%s", self.name, self.state.to_dict())

        try:
            success = self.funcs["inject"](self.name)
        except Exception as e:
            self.state.injection_failures += 1
            logging.error("Injection exception in event '%s': %s", self.name, e, exc_info=True)
            logging.debug("Injection error | event=%s | state=%s", self.name, self.state.to_dict())
            return Outcome.ERROR, f"injection_error ({self.state.injection_failures}x)"

        self.state.injected = success
        if success:
            logging.info("Injection for event '%s' succeeded", self.name)
            logging.debug("Injection success | event=%s | state=%s", self.name, self.state.to_dict())
            return Outcome.SUCCESS, "injection_ok"

        self.state.injection_failures += 1
        logging.warning(
            "Injection for event '%s' failed (%d/%d)",
            self.name, self.state.injection_failures, self.max_failed_injections
        )
        logging.debug("Injection fail | event=%s | state=%s", self.name, self.state.to_dict())
        return Outcome.FAILURE, f"failed_injection ({self.state.injection_failures}x)"

    # ---------------- Transitions & Reset ----------------

    def _apply_success_transitions(self) -> Optional[str]:
        """
        Handle warmup, cooldown, and reset-on-verify transitions after a SUCCESSFUL detection.
        Returns an optional info string to log (e.g., cooldown progress).
        """
        # Warmup handling
        if self.state.in_warmup:
            self.state.warmup_counter += 1
            if self.state.warmup_counter >= self.warmup.success_threshold:
                self.state.in_warmup = False
                self.state.warmup_counter = 0
                return f"Event '{self.name}' exited warmup after {self.warmup.success_threshold} consecutive successes"
            else:
                return (f"Detection for event '{self.name}' succeeded "
                        f"{self.state.warmup_counter}x in warmup (need {self.warmup.success_threshold})")

        # Post-success reset behavior
        if self.reset.on_verify:
            if self.reset.mode == "fast":
                # full reset but preserve warmup flag if enabled in config
                keep_warmup = self.warmup.enabled
                new_state = EventState(in_warmup=keep_warmup)
                self.state = new_state
                return None
            elif self.reset.mode == "cooldown" and self.state.alerted:
                self.state.cooldown_counter += 1
                if self.state.cooldown_counter < self.reset.cooldown_threshold:
                    return f"Event '{self.name}' in cooldown ({self.state.cooldown_counter}/{self.reset.cooldown_threshold})"
                # cooldown complete → full reset
                keep_warmup = self.warmup.enabled
                self.state = EventState(in_warmup=keep_warmup)
                return f"Event '{self.name}' cooldown complete → state fully reset"
        return None

    def check_alert(self) -> Optional[str]:
        if self.state.alerted:
            return None
        if (self.state.detection_failures >= self.max_failed_detections or
            self.state.injection_failures >= self.max_failed_injections):
            self.state.alerted = True
            return (f"DIVA Alert: event '{self.name}' failures → "
                    f"detection: {self.state.detection_failures}, "
                    f"injection: {self.state.injection_failures}")
        return None

    # ---------------- Cycle ----------------

    def run_cycle(self) -> Tuple[Dict[str, str], EventState]:
        """
        Executes one DIVA cycle for the event:
          Detect → (apply success transitions) → Optional Inject → Alert check → Status formatting
        """
        results: Dict[str, str] = {}

        det_outcome = self.detect()

        # Handle detection outcomes
        if det_outcome == Outcome.SUCCESS:
            logging.info("Detection for event '%s' succeeded", self.name)
            note = self._apply_success_transitions()
            if note:
                logging.info(note)
            results[self.name] = (
                "warmup_success"
                if self.state.in_warmup
                else "ok"
            )

        elif det_outcome == Outcome.FAILURE:
            results[self.name] = (
                "failed_detection (warmup)"
                if self.state.in_warmup
                else f"failed_detection ({self.state.detection_failures}/{self.max_failed_detections})"
            )

        elif det_outcome == Outcome.ERROR:
            if self.state.in_warmup:
                results[self.name] = "detection_error (warmup)"
            else:
                results[self.name] = f"detection_error ({self.state.detection_failures}/{self.max_failed_detections})"

        elif det_outcome == Outcome.SKIPPED:
            results[self.name] = "skipped (role=inject)"

        # Optional injection (monolithic both/inject or distributed inject)
        inj_outcome, inj_status = self.inject()
        if inj_outcome != Outcome.SKIPPED:
            results[self.name] = inj_status

        # Alerting (unified)
        alert_msg = self.check_alert()
        if alert_msg:
            if "alert" in self.funcs and callable(self.funcs["alert"]):
                try:
                    logging.info("Alerting for event '%s'", self.name)
                    self.funcs["alert"](alert_msg)
                    logging.info("Event '%s' alerted", self.name)
                except Exception:
                    logging.error("Alert function failed for event '%s'", self.name, exc_info=True)
            else:
                logging.error(alert_msg)

        return results, self.state


# =============================================================================
# Orchestrator
# =============================================================================

class DivaEngine:
    def __init__(self, mode: str = DIVA_MODE):
        self.mode = mode
        if self.mode == "monolithic":
            self.backend: StateBackend = S3Backend(STATE_BUCKET, STATE_KEY)
        else:
            self.backend = DynamoBackend(DDB_TABLE)

    def _load_event_state(self, name: str, warmup_enabled: bool, full_state: Optional[Dict[str, Any]]) -> EventState:
        if self.mode == "monolithic":
            raw = (full_state or {}).get(name, {})
        else:
            raw = self.backend.load_one(name)
        return EventState.from_dict(raw, warmup_enabled)

    def _save_event_state(self, name: str, state: EventState, full_state: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if self.mode == "monolithic":
            full_state = full_state or {}
            full_state[name] = state.to_dict()
            return full_state
        else:
            self.backend.save_one(name, state.to_dict())
            return None

    def _build_event(self, name: str, funcs: Dict[str, Any], state: EventState) -> DivaEvent:
        return DivaEvent(name, funcs, state)

    def process_serial(self, events: Dict[str, Dict[str, Any]], full_state: Optional[Dict[str, Any]]) -> Tuple[Dict[str, str], Optional[Dict[str, Any]]]:
        results: Dict[str, str] = {}
        for name, funcs in events.items():
            warmup_enabled = bool(funcs.get("warmup", {}).get("enabled", False))
            state = self._load_event_state(name, warmup_enabled, full_state)

            ev = self._build_event(name, funcs, state)
            event_result, new_state = ev.run_cycle()
            results.update(event_result)

            full_state = self._save_event_state(name, new_state, full_state)
        return results, full_state

    def process_parallel(self, events: Dict[str, Dict[str, Any]], full_state: Optional[Dict[str, Any]]) -> Tuple[Dict[str, str], Optional[Dict[str, Any]]]:
        results: Dict[str, str] = {}

        def _work(name: str, funcs: Dict[str, Any]) -> Tuple[str, Dict[str, str], EventState]:
            warmup_enabled = bool(funcs.get("warmup", {}).get("enabled", False))
            state = self._load_event_state(name, warmup_enabled, full_state)
            ev = self._build_event(name, funcs, state)
            res, new_state = ev.run_cycle()
            return name, res, new_state

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(_work, n, f): n for n, f in events.items()}
            for fut in as_completed(futures):
                try:
                    name, res, new_state = fut.result()
                    results.update(res)
                    _ = self._save_event_state(name, new_state, full_state)
                except Exception as e:
                    logging.error("Error processing event '%s': %s", futures[fut], e, exc_info=True)

        return results, full_state

    def run(self) -> Dict[str, str]:
        setup_logging()
        logging.debug("Starting DIVA Engine")

        logging.info("Getting events...")
        events = event_logic.get_events()
        logging.info("Retrieved %d events", len(events))

        if self.mode == "monolithic":
            logging.debug("Loading state from S3")
            full_state = self.backend.load_all()
            logging.info("Loaded state for %d events", len(full_state))
            if PARALLELIZE:
                logging.debug("Parallelizing events")
                results, new_state = self.process_parallel(events, full_state)
            else:
                logging.debug("Serializing events")
                results, new_state = self.process_serial(events, full_state)
            logging.debug("Saving state to S3")
            self.backend.save_all(new_state or {})
            logging.info("Saved event state for %d events", len(new_state or {}))
        else:
            # distributed → per-event in DynamoDB
            if PARALLELIZE:
                results, _ = self.process_parallel(events, None)
            else:
                results, _ = self.process_serial(events, None)

        logging.info("Results %s", str(results))
        logging.info("DIVA Engine complete")
        return results


# =============================================================================
# Lambda Entrypoint
# =============================================================================

def lambda_handler(_, __):
    engine = DivaEngine(mode=DIVA_MODE)
    return engine.run()
