"""Unit tests for AWSScanner (Cloud Discovery Agent's read-only boto3 layer).

Uses moto to fully mock AWS - no real AWS calls are made anywhere in this file.
"""

import json

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws

from tools.aws_scanner import AWSScanner, RETRY_CONFIG, _new_retry_config

FAKE_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
FAKE_SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


@mock_aws
def test_scan_vpcs_discovers_created_vpc():
    scanner = AWSScanner(access_key=FAKE_ACCESS_KEY, secret_key=FAKE_SECRET_KEY, region="us-east-1")
    ec2 = boto3.client("ec2", region_name="us-east-1")
    created = ec2.create_vpc(CidrBlock="10.0.0.0/16")
    vpc_id = created["Vpc"]["VpcId"]

    results = scanner.scan_vpcs()

    assert len(results) >= 1
    found = [r for r in results if r["id"] == vpc_id]
    assert found
    assert found[0]["cidr_block"] == "10.0.0.0/16"
    assert found[0]["resource_type"] == "aws_vpc"


@mock_aws
def test_scan_ec2_instances_excludes_terminated(monkeypatch):
    scanner = AWSScanner(access_key=FAKE_ACCESS_KEY, secret_key=FAKE_SECRET_KEY, region="us-east-1")
    ec2 = boto3.client("ec2", region_name="us-east-1")

    images = ec2.describe_images()["Images"]
    ami_id = images[0]["ImageId"] if images else "ami-12345678"

    run = ec2.run_instances(ImageId=ami_id, MinCount=1, MaxCount=1, InstanceType="t2.micro")
    running_id = run["Instances"][0]["InstanceId"]

    run2 = ec2.run_instances(ImageId=ami_id, MinCount=1, MaxCount=1, InstanceType="t2.micro")
    terminated_id = run2["Instances"][0]["InstanceId"]
    ec2.terminate_instances(InstanceIds=[terminated_id])

    results = scanner.scan_ec2_instances()
    discovered_ids = {r["id"] for r in results}

    assert running_id in discovered_ids
    assert terminated_id not in discovered_ids


@mock_aws
def test_scan_all_aggregates_across_pagination(monkeypatch):
    """Verifies scan_vpcs aggregates results across multiple paginator pages,
    not just the first page - simulated by monkeypatching the paginator to
    yield two pages instead of relying on moto to create 1000s of VPCs."""
    scanner = AWSScanner(access_key=FAKE_ACCESS_KEY, secret_key=FAKE_SECRET_KEY, region="us-east-1")
    ec2 = boto3.client("ec2", region_name="us-east-1", config=_new_retry_config())

    page_1 = {"Vpcs": [{"VpcId": "vpc-page1", "CidrBlock": "10.1.0.0/16", "Tags": []}]}
    page_2 = {"Vpcs": [{"VpcId": "vpc-page2", "CidrBlock": "10.2.0.0/16", "Tags": []}]}

    class FakePaginator:
        def paginate(self):
            return iter([page_1, page_2])

    class FakeEC2:
        def get_paginator(self, name):
            assert name == "describe_vpcs"
            return FakePaginator()

    monkeypatch.setattr(scanner.session, "client", lambda *a, **kw: FakeEC2())

    results = scanner.scan_vpcs()
    ids = {r["id"] for r in results}
    assert ids == {"vpc-page1", "vpc-page2"}


def test_retry_config_uses_standard_mode_with_five_attempts():
    """The scanner must configure botocore's built-in retry handler (rather
    than reinventing retry logic) so transient rate-limit errors like
    RequestLimitExceeded are retried automatically before giving up."""
    assert RETRY_CONFIG.retries["mode"] == "standard"
    assert RETRY_CONFIG.retries["max_attempts"] == 5


def test_scan_vpcs_degrades_gracefully_after_retries_exhausted():
    """Once retries are exhausted, describe_vpcs raises ClientError all the
    way up to our code. scan_vpcs must catch it and return an empty list
    rather than crashing the whole discovery agent over one resource type."""
    scanner = AWSScanner(access_key=FAKE_ACCESS_KEY, secret_key=FAKE_SECRET_KEY, region="us-east-1")
    real_client = scanner.session.client("ec2", config=_new_retry_config(), region_name="us-east-1")
    stubber = Stubber(real_client)

    # Queue enough throttling errors to exhaust all retry attempts.
    for _ in range(6):
        stubber.add_client_error(
            "describe_vpcs",
            service_error_code="RequestLimitExceeded",
            service_message="Request limit exceeded.",
            http_status_code=503,
        )

    scanner.session.client = lambda *a, **kw: real_client

    with stubber:
        results = scanner.scan_vpcs()

    assert results == []


@mock_aws
def test_credentials_never_appear_in_scan_results():
    """The most important security property of the scanner: no matter what
    resources are discovered, the raw credentials used to discover them must
    never leak into the returned resource data."""
    scanner = AWSScanner(access_key=FAKE_ACCESS_KEY, secret_key=FAKE_SECRET_KEY, region="us-east-1")
    ec2 = boto3.client("ec2", region_name="us-east-1")
    ec2.create_vpc(CidrBlock="10.0.0.0/16")
    ec2.create_security_group(GroupName="test-sg", Description="test")

    results = scanner.scan_all(filters=["VPC", "SG"])
    serialized = json.dumps(results, default=str)

    assert FAKE_ACCESS_KEY not in serialized
    assert FAKE_SECRET_KEY not in serialized
