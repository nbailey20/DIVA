"""
event_logic.py
DIVA Lambda Engine - Event Logic Skeleton

Customize this file with your own detect/inject/alert logic.
"""

# ----------------------
# Example helper functions
# ----------------------

def detect(event_name: str) -> bool:
    """
    Example detection logic.
    Return True if the event is healthy, False otherwise.
    """
    print(f"[DETECT] Checking event '{event_name}'...")
    return True  # Replace with real detection logic


def inject(event_name: str) -> bool:
    """
    Example injection logic.
    Return True if injection succeeded, False otherwise.
    """
    print(f"[INJECT] Injecting event '{event_name}'...")
    return True  # Replace with real injection logic


def alert(message: str):
    """Default: Just log the alert (always available)."""
    print(f"[ALERT] {message}")


# ----------------------
# Schema validation
# ----------------------

REQUIRED_KEYS = {"alert"}
ROLE_REQUIREMENTS = {
    "detect": {"detect"},
    "inject": {"inject"},
    "both": {"detect", "inject"},
}

def validate_event_schema(event_name: str, config: dict):
    """Validate an event config dictionary."""
    role = config.get("role", "both")

    # Ensure required keys
    missing = REQUIRED_KEYS | (ROLE_REQUIREMENTS.get(role, set()))
    if not missing.issubset(config.keys()):
        raise ValueError(
            f"Event '{event_name}' is missing required keys for role='{role}': "
            f"{missing - config.keys()}"
        )

    # Ensure callables
    for key in ["detect", "inject", "alert"]:
        if key in config and not callable(config[key]):
            raise TypeError(f"Event '{event_name}': '{key}' must be a function")


# ----------------------
# Main entrypoint
# ----------------------

def get_events():
    """
    Return a dictionary of all events to monitor.
    Each event must define at least `alert` and the required functions for its role.
    """

    events = {
        "event_1": {
            "detect": detect,
            "inject": inject,
            "alert": alert,           # choose alert_to_sns or alert_to_slack
            "role": "both",
            "max_failed_detections": 3,
            "max_failed_injections": 1,
            "inject_each_period": True,
            "reset": {
                "mode": "cooldown",
                "on_verify": False,
                "cooldown_success_threshold": 2,
            },
        },
        "event_2": {
            "inject": inject,
            "alert": alert,
            "role": "inject",
            "max_failed_injections": 2,
        },
        "event_3": {
            "detect": detect,
            "alert": alert,
            "role": "detect",
            "max_failed_detections": 4,
        },
    }

    # Validate all events before returning
    for name, cfg in events.items():
        validate_event_schema(name, cfg)

    return events
