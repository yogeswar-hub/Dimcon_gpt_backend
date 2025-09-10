import os
import sys

import boto3
from botocore.exceptions import ClientError

USER_POOL_ID = os.environ.get("USER_POOL_ID")
REGION = os.environ.get("REGION")
cognito_client = boto3.client("cognito-idp", region_name=REGION)

sys.path.append(".")

from app.user import User


def _create_email_from_id(user_id: str) -> str:
    return f"{user_id}@example.com"


def store_user_in_cognito(user: User) -> None:
    try:
        cognito_client.admin_create_user(
            UserPoolId=USER_POOL_ID,
            Username=user.email,
            UserAttributes=[
                {"Name": "name", "Value": user.email},
            ],
            DesiredDeliveryMediums=["EMAIL"],
            ForceAliasCreation=False,
            MessageAction="SUPPRESS",  # Suppresses the welcome email
        )
        print(f"User '{user.name}' saved in Cognito.")
    except cognito_client.exceptions.UsernameExistsException:
        print(f"User '{user.name}' already exists in Cognito.")

    try:
        for group in user.groups:
            cognito_client.admin_add_user_to_group(
                UserPoolId=USER_POOL_ID, Username=user.email, GroupName=group
            )
            print(f"User '{user.name}' added to group '{group}'.")
    except ClientError as e:
        print(f"Error adding user to group: {e}")
        raise e


def delete_cognito_user(user: User):
    try:
        cognito_client.admin_delete_user(UserPoolId=USER_POOL_ID, Username=user.email)
        print(f"User '{user.name}' deleted from Cognito.")
    except ClientError as e:
        print(f"Error deleting user from Cognito: {e}")
        raise e


def store_group_in_cognito(group_name: str, description: str = "") -> None:
    """
    Create a group in Cognito if it doesn't already exist.
    """
    try:
        cognito_client.create_group(
            GroupName=group_name,
            UserPoolId=USER_POOL_ID,
            Description=description,
        )
        print(f"Group '{group_name}' created in Cognito.")
    except cognito_client.exceptions.GroupExistsException:
        print(f"Group '{group_name}' already exists in Cognito.")
    except ClientError as e:
        print(f"Error creating group in Cognito: {e}")
        raise e


def delete_cognito_group(group_name: str) -> None:
    """
    Delete a group in Cognito.
    """
    try:
        cognito_client.delete_group(GroupName=group_name, UserPoolId=USER_POOL_ID)
        print(f"Group '{group_name}' deleted from Cognito.")
    except ClientError as e:
        print(f"Error deleting group from Cognito: {e}")
        raise e


def create_test_user(user_name: str, groups=[]) -> User:
    return User(
        id=user_name,
        name=user_name,
        email=_create_email_from_id(user_name),
        groups=groups,
    )


def create_test_admin_user(user_name: str) -> User:
    return User(
        id=user_name,
        name=user_name,
        email=_create_email_from_id(user_name),
        groups=["Admin"],
    )
