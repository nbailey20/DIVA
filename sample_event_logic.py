import logging

def detect(event_name):
    logging.info("Detecting event %s", event_name)
    return False  # Simulate detection failure

def inject(event_name):
    logging.info("Injecting event %s", event_name)

def alert(msg):
    logging.error("[ALERT] %s", msg)


events = {
  "test_event1": {
    "detect": detect,
    "inject": inject,
    "alert": alert,
    "max_failures": 3,  # allow 3 consecutive misses before alert
    "inject_each_period": True  # default behavior: False, inject only on first failure
  },
  "test_event2": {
    "detect": detect,
    "inject": inject,
    "alert": alert,
    "max_failures": 2,
    "inject_each_period": False
  },
  "test_event3": {
    "role": "detect",  # only execute detection logic
    "detect": detect,
    "alert": alert,
    "max_failures": 5,
    "inject_each_period": True
  },
   "test_event4": {
    "role": "inject",
    "inject": inject,
    # "alert": alert,
    "max_failures": 5,
    "inject_each_period": False
  }
}

def get_events() -> dict:
    return events
