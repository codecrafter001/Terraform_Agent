"""Unit tests for tools/aws_live_fetch.py - mocks the boto3 client so these
run without real AWS credentials. Focuses on: correct field-shape mapping
(matching tools/aws_scanner.py's scan_* output shape, since
drift_reconciliation_agent diffs same-named fields directly), and that a
deleted resource (empty response / NotFound error) is reported as None, not
raised as an exception."""

from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from tools.aws_live_fetch import fetch_live_instance, fetch_live_resource, fetch_live_vpc


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "not found"}}, "DescribeVpcs")


def test_fetch_live_vpc_returns_matching_field_shape():
    fake_session = MagicMock()
    fake_ec2 = MagicMock()
    fake_session.client.return_value = fake_ec2
    fake_ec2.describe_vpcs.return_value = {
        "Vpcs": [{"VpcId": "vpc-1", "CidrBlock": "10.0.0.0/16", "IsDefault": False, "Tags": [{"Key": "Name", "Value": "x"}]}]
    }

    result = fetch_live_vpc(fake_session, "vpc-1")

    assert result == {
        "resource_type": "aws_vpc", "id": "vpc-1", "name": "x",
        "cidr_block": "10.0.0.0/16", "is_default": False, "tags": [{"Key": "Name", "Value": "x"}],
    }
    fake_ec2.describe_vpcs.assert_called_once_with(VpcIds=["vpc-1"])


def test_fetch_live_vpc_returns_none_when_deleted():
    fake_session = MagicMock()
    fake_ec2 = MagicMock()
    fake_session.client.return_value = fake_ec2
    fake_ec2.describe_vpcs.side_effect = _client_error("InvalidVpcID.NotFound")

    assert fetch_live_vpc(fake_session, "vpc-gone") is None


def test_fetch_live_vpc_returns_none_when_empty_result():
    fake_session = MagicMock()
    fake_ec2 = MagicMock()
    fake_session.client.return_value = fake_ec2
    fake_ec2.describe_vpcs.return_value = {"Vpcs": []}

    assert fetch_live_vpc(fake_session, "vpc-1") is None


def test_fetch_live_instance_skips_terminated_state():
    fake_session = MagicMock()
    fake_ec2 = MagicMock()
    fake_session.client.return_value = fake_ec2
    fake_ec2.describe_instances.return_value = {
        "Reservations": [{"Instances": [{"InstanceId": "i-1", "State": {"Name": "terminated"}}]}]
    }

    assert fetch_live_instance(fake_session, "i-1") is None


def test_fetch_live_resource_dispatches_by_type():
    fake_session = MagicMock()
    fake_ec2 = MagicMock()
    fake_session.client.return_value = fake_ec2
    fake_ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-1", "CidrBlock": "10.0.0.0/16", "Tags": []}]}

    result = fetch_live_resource(fake_session, "aws_vpc", "vpc-1")
    assert result["resource_type"] == "aws_vpc"


def test_fetch_live_resource_unknown_type_returns_none_without_calling_aws():
    fake_session = MagicMock()
    result = fetch_live_resource(fake_session, "aws_lambda_function", "fn-1")
    assert result is None
    fake_session.client.assert_not_called()
