import logging
import datetime
import json

import boto3

cloudwatch = boto3.client("cloudwatch")
eventbridge = boto3.client("events")
lambda_client = boto3.client("lambda")


def detect_lambda_activity(lambda_name: str, lookback_minutes: int = 2) -> bool:
    """
    Detect whether a Lambda has processed at least one successful invocation
    in the past `lookback_minutes` using a single CloudWatch API call.
    """
    end = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(minutes=lookback_minutes)

    metrics = [
        {
            "Id": "invocations",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/Lambda",
                    "MetricName": "Invocations",
                    "Dimensions": [{"Name": "FunctionName", "Value": lambda_name}]
                },
                "Period": lookback_minutes * 60,
                "Stat": "Sum"
            },
            "ReturnData": True
        },
        {
            "Id": "errors",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/Lambda",
                    "MetricName": "Errors",
                    "Dimensions": [{"Name": "FunctionName", "Value": lambda_name}]
                },
                "Period": lookback_minutes * 60,
                "Stat": "Sum"
            },
            "ReturnData": True
        }
    ]

    resp = cloudwatch.get_metric_data(
        MetricDataQueries=metrics,
        StartTime=start,
        EndTime=end
    )

    invocations = 0
    errors = 0

    for result in resp.get("MetricDataResults", []):
        if result["Id"] == "invocations":
            invocations = sum(result.get("Values", []))
        elif result["Id"] == "errors":
            errors = sum(result.get("Values", []))

    return invocations - errors > 0



def inject_to_lambda(lambda_name: str) -> bool:
    """
    Invoke a lambda with a test event.
    """
    try:
        response = lambda_client.invoke(
            FunctionName=lambda_name,
            InvocationType="Event",  # async
            Payload=json.dumps({"event_name": lambda_name}).encode("utf-8")
        )
        status_code = response.get("StatusCode", 0)
        if 200 <= status_code < 300:
            print(f"Injected event '{lambda_name}' to Lambda '{lambda_name}'")
            return True
        else:
            print(f"Failed injection to Lambda '{lambda_name}': {response}")
            return False
    except Exception as e:
        print(f"Exception injecting event '{lambda_name}' to Lambda '{lambda_name}': {e}")
        return False


def alert(msg):
    logging.error("[DIVA HAS ALERTED!!!] %s", msg)


events = {}


def get_events() -> dict:
    return events
