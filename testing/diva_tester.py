import argparse
import json
import io
import zipfile
from time import sleep
import boto3


lambda_client = boto3.client("lambda")
iam = boto3.client("iam")
sts = boto3.client("sts")
account_id = sts.get_caller_identity()["Account"]
region = boto3.session.Session().region_name

IAM_ROLE_NAME = "LambdaInvokeRole"

def create_iam_role():
    """
    Creates an IAM role that allows a Lambda function to invoke other functions.
    """
    role_name = IAM_ROLE_NAME
    assume_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
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
                    "Action": "lambda:InvokeFunction",
                    "Resource": "*"
                },
                {
                    "Effect": "Allow",
                    "Action": "logs:*",
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


# Simple Lambda function code template
LAMBDA_CODE = """
import os
import boto3

lambda_client = boto3.client("lambda")

def lambda_handler(event, context):
    children = os.environ.get("CHILD_LAMBDAS", "").split(",")
    for child in children:
        if child:
            lambda_client.invoke(FunctionName=child, InvocationType="Event")
            print(f"Invoked {child}")
    return {"status": "ok", "invoked_children": children}
"""

def create_zip_from_code(code_str):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("lambda_function.py", code_str)
    buf.seek(0)
    return buf.read()


def create_lambda_chain(chain_spec, role_arn, base_name="node"):
    """
    Create a fan-out Lambda chain where each function knows the ARNs of its children.

    Args:
        chain_spec (list[int]): Number of lambdas at each level, e.g., [1, 2, 1]
        role_arn (str): IAM role ARN for all lambdas
        base_name (str): Base name for lambda functions

    Returns:
        list[str]: All Lambda ARNs in the chain
    """
    lambdas_by_level = []
    all_lambdas = []
    zip_bytes = create_zip_from_code(LAMBDA_CODE)

    # Precompute "names" for all lambdas at each level so we can construct ARNs
    lambda_names_by_level = []
    for level_idx, count in enumerate(chain_spec):
        level_names = [f"{base_name}-{level_idx}-{node_idx}" for node_idx in range(count)]
        lambda_names_by_level.append(level_names)

    # Now create all functions in order
    for level_idx, count in enumerate(chain_spec):
        level_lambdas = []
        for node_idx in range(count):
            lambda_name = lambda_names_by_level[level_idx][node_idx]

            # Compute children ARNs for next level (empty for last level)
            if level_idx + 1 < len(chain_spec):
                next_level_names = lambda_names_by_level[level_idx + 1]
                children_str = ",".join(
                    f"arn:aws:lambda:{lambda_client.meta.region_name}:"
                    f"{boto3.client('sts').get_caller_identity()['Account']}:function:{name}:$LATEST"
                    for name in next_level_names
                )
            else:
                children_str = ""

            response = lambda_client.create_function(
                FunctionName=lambda_name,
                Runtime="python3.13",
                Role=role_arn,
                Handler="lambda_function.lambda_handler",
                Code={"ZipFile": zip_bytes},
                Timeout=60,
                MemorySize=128,
                Environment={"Variables": {"CHILD_LAMBDAS": children_str}}
            )

            lambda_arn = response["FunctionArn"]
            level_lambdas.append(lambda_arn)
            all_lambdas.append(lambda_arn)
            print(f"✅ Created Lambda: {lambda_name} with children: {children_str.split(',') if children_str else '[]'}")

        lambdas_by_level.append(level_lambdas)

    print("✅ Lambda chain created successfully")
    return all_lambdas



def delete_lambda_chain(chain_spec, base_name="node"):
    """
    Delete all Lambda functions created by create_lambda_chain.

    Args:
        chain_spec (list[int]): Number of Lambdas per level, e.g., [1,2,1].
        base_name (str): Base name used in create_lambda_chain (default "node").
    """
    lambda_client = boto3.client("lambda")

    for level_idx, count in enumerate(chain_spec):
        for node_idx in range(count):
            lambda_name = f"{base_name}-{level_idx}-{node_idx}"
            try:
                lambda_client.delete_function(FunctionName=lambda_name)
                print(f"✅ Deleted Lambda: {lambda_name}")
            except lambda_client.exceptions.ResourceNotFoundException:
                print(f"⚠️ Lambda not found (already deleted?): {lambda_name}")
            except Exception as e:
                print(f"❌ Failed to delete Lambda {lambda_name}: {e}")
    print("🗑️  Chain deleted successfully")



# -------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build or delete EventBridge bus chains")
    parser.add_argument("chain", help="Comma-separated list of ints (e.g. 2,3,1,1)")
    parser.add_argument("-d", "--delete", action="store_true", help="Delete the chain instead of creating it")
    args = parser.parse_args()

    chain_spec = [int(x) for x in args.chain.split(",")]

    if args.delete:
        delete_lambda_chain(chain_spec)
        delete_iam_role()
    else:
        role_arn = create_iam_role()
        print("Waiting 10 seconds for IAM role to propagate...")
        sleep(10)  # Wait for IAM role to propagate
        create_lambda_chain(chain_spec, role_arn)
