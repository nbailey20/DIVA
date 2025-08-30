import argparse
import json
import boto3

client = boto3.client("events")
iam = boto3.client("iam")

IAM_ROLE_NAME = "EventBridgeCrossBusRole"


def create_iam_role():
    """
    Creates an IAM role that allows EventBridge to put events on other buses.
    """
    role_name = IAM_ROLE_NAME
    assume_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "events.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }
        ]
    }

    response = iam.create_role(
        RoleName=role_name,
        AssumeRolePolicyDocument=json.dumps(assume_policy)
    )
    role_arn = response["Role"]["Arn"]

    # Attach permission to put events on all buses (or narrow it to specific buses)
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="AllowPutEvents",
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "events:PutEvents",
                    "Resource": "*"
                }
            ]
        })
    )
    print(f"✅ IAM role {role_name} created successfully")
    return role_arn


def delete_iam_role():
    """
    Deletes the IAM role created for EventBridge cross-bus access.
    """
    role_name = IAM_ROLE_NAME
    try:
        iam.delete_role_policy(RoleName=role_name, PolicyName="AllowPutEvents")
    except iam.exceptions.NoSuchEntityException:
        pass
    try:
        iam.delete_role(RoleName=role_name)
    except iam.exceptions.NoSuchEntityException:
        pass
    print("🗑️  IAM role deleted successfully")


def create_chain(chain_spec, role_arn):
    """
    Creates a chain of EventBridge buses according to chain_spec.
    Example: [2,3,1,1] => (A,A)->(B,B,B)->C->D
    """
    buses_by_level = []
    
    for i, count in enumerate(chain_spec):
        level_buses = []
        for j in range(count):
            bus_name = f"chain-level-{i}-bus-{j}"
            print(f"Creating bus: {bus_name}")
            client.create_event_bus(Name=bus_name)
            level_buses.append(bus_name)
        buses_by_level.append(level_buses)

    # Connect buses by rules
    for i in range(len(buses_by_level) - 1):
        for src_bus in buses_by_level[i]:
            for dst_bus in buses_by_level[i+1]:
                rule_name = f"{src_bus}-to-{dst_bus}"
                print(f"Creating rule {rule_name} from {src_bus} -> {dst_bus}")
                client.put_rule(
                    Name=rule_name,
                    EventBusName=src_bus,
                    EventPattern=json.dumps({"source": ["*"]})  # match all events
                )
                client.put_targets(
                    Rule=rule_name,
                    EventBusName=src_bus,
                    Targets=[
                        {
                            "Id": dst_bus,
                            "Arn": f"arn:aws:events:{client.meta.region_name}:{boto3.client('sts').get_caller_identity()['Account']}:event-bus/{dst_bus}",
                            "RoleArn": role_arn
                        }
                    ]
                )

    print("✅ Chain created successfully")
    return buses_by_level


def delete_chain(chain_spec):
    """
    Deletes all buses and rules created for the chain.
    """
    buses_by_level = []
    for i, count in enumerate(chain_spec):
        buses_by_level.append([f"chain-level-{i}-bus-{j}" for j in range(count)])

    # Delete rules and targets
    for i in range(len(buses_by_level) - 1):
        for src_bus in buses_by_level[i]:
            for dst_bus in buses_by_level[i+1]:
                rule_name = f"{src_bus}-to-{dst_bus}"
                print(f"Deleting rule {rule_name}")
                try:
                    client.remove_targets(Rule=rule_name, EventBusName=src_bus, Ids=[dst_bus])
                except client.exceptions.ResourceNotFoundException:
                    pass
                try:
                    client.delete_rule(Name=rule_name, EventBusName=src_bus)
                except client.exceptions.ResourceNotFoundException:
                    pass

    # Delete buses
    for level in reversed(buses_by_level):
        for bus in level:
            print(f"Deleting bus {bus}")
            try:
                client.delete_event_bus(Name=bus)
            except client.exceptions.ResourceNotFoundException:
                pass

    print("🗑️  Chain deleted successfully")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build or delete EventBridge bus chains")
    parser.add_argument("chain", help="Comma-separated list of ints (e.g. 2,3,1,1)")
    parser.add_argument("-d", "--delete", action="store_true", help="Delete the chain instead of creating it")
    args = parser.parse_args()

    chain_spec = [int(x) for x in args.chain.split(",")]

    if args.delete:
        delete_chain(chain_spec)
        delete_iam_role()
    else:
        role_arn = create_iam_role()
        create_chain(chain_spec, role_arn)
