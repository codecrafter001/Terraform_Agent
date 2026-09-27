"""Discovery and adoption templates for IGW, NAT, load balancers, DynamoDB, KMS,
SQS and SNS (tools/aws_scanner_services.py + tools/hcl_generator.py).

moto emulates AWS - no real AWS calls. The rule under test throughout: every
value in the adoption code is the live value; a read that fails makes the scan
INCOMPLETE and leaves the value out, never replaced by a guess."""

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from tools.aws_scanner import AWSScanner
from tools.hcl_generator import HCLGenerator

FAKE_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
FAKE_SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
REGION = "us-east-1"


def _scanner() -> AWSScanner:
    return AWSScanner(access_key=FAKE_ACCESS_KEY, secret_key=FAKE_SECRET_KEY, region=REGION)


def _denied(operation: str) -> ClientError:
    return ClientError({"Error": {"Code": "AccessDenied", "Message": "not allowed"}}, operation)


class _FailingCalls:
    """A boto client whose named operations raise AccessDenied."""

    def __init__(self, client, failing):
        self._client, self._failing = client, set(failing)

    def __getattr__(self, name):
        if name in self._failing:
            def fail(*_a, **_kw):
                raise _denied(name)
            return fail
        return getattr(self._client, name)


def _with_failing(scanner: AWSScanner, service: str, *operations: str) -> None:
    real = scanner._client

    def client(name: str):
        c = real(name)
        return _FailingCalls(c, operations) if name == service else c

    scanner._client = client  # type: ignore[method-assign]


def _generate(resources):
    gen = HCLGenerator(job_id="job-extra", region=REGION, engine_name="terraform", engine_version="1.8.0")
    files, manifest = gen.generate_project(resources=resources, classification_results={},
                                           adoption_plan={}, dependency_graph={})
    return manifest, "\n".join(v for k, v in files.items() if k.endswith(".tf"))


# --------------------------------------------------------------------------- discovery


@mock_aws
def test_dynamodb_records_keys_capacity_stream_and_tags():
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName="orders",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "ts", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "ts", "AttributeType": "N"}],
        ProvisionedThroughput={"ReadCapacityUnits": 7, "WriteCapacityUnits": 3},
        StreamSpecification={"StreamEnabled": True, "StreamViewType": "NEW_IMAGE"},
        Tags=[{"Key": "team", "Value": "payments"}],
    )
    scanner = _scanner()
    [table] = scanner.scan_dynamodb_tables()
    assert (table["hash_key"], table["hash_key_type"], table["range_key"], table["range_key_type"]) == ("pk", "S", "ts", "N")
    assert (table["billing_mode"], table["read_capacity"], table["write_capacity"]) == ("PROVISIONED", 7, 3)
    assert table["stream_enabled"] is True and table["stream_view_type"] == "NEW_IMAGE"
    assert table["tags"] == [{"Key": "team", "Value": "payments"}]
    assert scanner.report()["complete"] is True


@mock_aws
def test_kms_records_force_new_fields_rotation_and_converts_tags():
    kms = boto3.client("kms", region_name=REGION)
    # moto only echoes KeyUsage/MultiRegion when given (real AWS always returns them).
    key_id = kms.create_key(Description="app key", KeyUsage="ENCRYPT_DECRYPT", MultiRegion=False, Tags=[{"TagKey": "env", "TagValue": "prod"}])["KeyMetadata"]["KeyId"]
    kms.enable_key_rotation(KeyId=key_id)
    scanner = _scanner()
    [key] = [k for k in scanner.scan_kms_keys() if k["id"] == key_id]
    assert key["key_manager"] == "CUSTOMER" and key["origin"] == "AWS_KMS"
    assert key["key_usage"] == "ENCRYPT_DECRYPT" and key["customer_master_key_spec"] == "SYMMETRIC_DEFAULT"
    assert key["multi_region"] is False and key["is_enabled"] is True
    assert key["enable_key_rotation"] is True
    assert key["tags"] == [{"Key": "env", "Value": "prod"}]


@mock_aws
def test_sqs_and_sns_record_attributes_and_tags():
    sqs = boto3.client("sqs", region_name=REGION)
    sqs.create_queue(QueueName="jobs.fifo", Attributes={"FifoQueue": "true", "ContentBasedDeduplication": "true",
                                                        "VisibilityTimeout": "45"}, tags={"team": "ops"})
    sqs.create_queue(QueueName="plain")
    boto3.client("sns", region_name=REGION).create_topic(Name="alerts", Tags=[{"Key": "team", "Value": "ops"}])

    scanner = _scanner()
    queues = {q["name"]: q for q in scanner.scan_sqs_queues()}
    assert queues["jobs.fifo"]["fifo_queue"] is True and queues["jobs.fifo"]["content_based_deduplication"] is True
    assert queues["jobs.fifo"]["visibility_timeout_seconds"] == 45
    assert queues["jobs.fifo"]["tags"] == [{"Key": "team", "Value": "ops"}]
    # content_based_deduplication is FIFO-only; a standard queue never carries it.
    assert queues["plain"]["fifo_queue"] is False and queues["plain"]["content_based_deduplication"] is None

    [topic] = scanner.scan_sns_topics()
    assert topic["name"] == "alerts" and topic["fifo_topic"] is False
    assert topic["tags"] == [{"Key": "team", "Value": "ops"}]


@mock_aws
def test_load_balancer_records_attributes_that_have_provider_defaults():
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc = ec2.create_vpc(CidrBlock="10.1.0.0/16")["Vpc"]["VpcId"]
    subnets = [ec2.create_subnet(VpcId=vpc, CidrBlock=f"10.1.{i}.0/24", AvailabilityZone=f"{REGION}{az}")["Subnet"]["SubnetId"]
               for i, az in ((1, "a"), (2, "b"))]
    elbv2 = boto3.client("elbv2", region_name=REGION)
    arn = elbv2.create_load_balancer(Name="web", Subnets=subnets, Tags=[{"Key": "app", "Value": "web"}])["LoadBalancers"][0]["LoadBalancerArn"]
    elbv2.modify_load_balancer_attributes(LoadBalancerArn=arn, Attributes=[
        {"Key": "deletion_protection.enabled", "Value": "true"}, {"Key": "idle_timeout.timeout_seconds", "Value": "120"}])

    [lb] = _scanner().scan_load_balancers()
    assert lb["id"] == arn and lb["name"] == "web" and lb["load_balancer_type"] == "application"
    assert sorted(lb["subnets"]) == sorted(subnets)
    assert lb["enable_deletion_protection"] is True and lb["idle_timeout"] == 120
    assert lb["tags"] == [{"Key": "app", "Value": "web"}]
    assert "enable_cross_zone_load_balancing" not in lb  # ALB-only attributes only


@mock_aws
def test_internet_gateway_on_the_default_vpc_is_marked_default():
    ec2 = boto3.client("ec2", region_name=REGION)
    default_vpc = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0]["VpcId"]
    other_vpc = ec2.create_vpc(CidrBlock="10.2.0.0/16")["Vpc"]["VpcId"]
    def_igw = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=def_igw, VpcId=default_vpc)
    custom = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=custom, VpcId=other_vpc)
    default_igw = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=default_igw, VpcId=default_vpc)

    igws = {g["id"]: g for g in _scanner().scan_internet_gateways()}
    assert igws[custom]["is_default"] is False and igws[custom]["vpc_id"] == other_vpc
    assert igws[default_igw]["is_default"] is True


@mock_aws
def test_failed_default_vpc_lookup_is_unknown_and_incomplete_not_custom():
    ec2 = boto3.client("ec2", region_name=REGION)
    ec2.create_internet_gateway()
    scanner = _scanner()
    _with_failing(scanner, "ec2", "describe_vpcs")
    igws = scanner.scan_internet_gateways()
    assert igws and all(g["is_default"] is None for g in igws)
    assert scanner.report()["complete"] is False


@pytest.mark.parametrize("service, operation, scan", [
    ("sqs", "list_queue_tags", "scan_sqs_queues"),
    ("sns", "list_tags_for_resource", "scan_sns_topics"),
    ("dynamodb", "list_tags_of_resource", "scan_dynamodb_tables"),
    ("kms", "list_resource_tags", "scan_kms_keys"),
    ("elbv2", "describe_tags", "scan_load_balancers"),
])
@mock_aws
def test_an_unreadable_tag_set_is_left_out_and_makes_the_scan_incomplete(service, operation, scan):
    """An empty tag set in place of an unread one would make the plan delete the live tags."""
    boto3.client("sqs", region_name=REGION).create_queue(QueueName="q", tags={"team": "ops"})
    boto3.client("sns", region_name=REGION).create_topic(Name="t", Tags=[{"Key": "team", "Value": "ops"}])
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName="t", KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST",
        Tags=[{"Key": "team", "Value": "ops"}])
    boto3.client("kms", region_name=REGION).create_key(Tags=[{"TagKey": "team", "TagValue": "ops"}])
    ec2 = boto3.client("ec2", region_name=REGION)
    vpc = ec2.create_vpc(CidrBlock="10.3.0.0/16")["Vpc"]["VpcId"]
    subnets = [ec2.create_subnet(VpcId=vpc, CidrBlock=f"10.3.{i}.0/24", AvailabilityZone=f"{REGION}{az}")["Subnet"]["SubnetId"]
               for i, az in ((1, "a"), (2, "b"))]
    boto3.client("elbv2", region_name=REGION).create_load_balancer(Name="lb", Subnets=subnets)

    scanner = _scanner()
    _with_failing(scanner, service, operation)
    records = getattr(scanner, scan)()
    assert records and all("tags" not in r for r in records)
    report = scanner.report()
    assert report["complete"] is False
    assert any(e["code"] == "AccessDenied" for e in report["errors"])


@mock_aws
def test_unreadable_kms_rotation_is_left_out_never_false():
    """enable_key_rotation = false in place of an unread value would switch rotation off."""
    key_id = boto3.client("kms", region_name=REGION).create_key()["KeyMetadata"]["KeyId"]
    scanner = _scanner()
    _with_failing(scanner, "kms", "get_key_rotation_status")
    [key] = [k for k in scanner.scan_kms_keys() if k["id"] == key_id]
    assert "enable_key_rotation" not in key
    assert scanner.report()["complete"] is False


# --------------------------------------------------------------------------- templates


def test_load_balancer_template_renders_live_attributes():
    manifest, hcl = _generate([{
        "id": "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/net/edge/abc",
        "resource_type": "aws_lb", "name": "edge", "scheme": "internal", "load_balancer_type": "network",
        "subnets": ["subnet-1"], "enable_deletion_protection": True, "enable_cross_zone_load_balancing": True,
    }])
    assert manifest.resources_generated == 1
    assert 'name               = "edge"' in hcl and "internal           = true" in hcl
    assert 'load_balancer_type = "network"' in hcl
    assert "enable_deletion_protection = true" in hcl and "enable_cross_zone_load_balancing = true" in hcl


def test_load_balancer_without_its_type_goes_to_review_not_a_guess():
    manifest, hcl = _generate([{"id": "arn:lb", "resource_type": "aws_lb", "name": "edge", "scheme": "internet-facing"}])
    assert manifest.resources_generated == 0 and manifest.resources_review_required == 1
    assert 'resource "aws_lb"' not in hcl


def test_kms_template_renders_force_new_fields():
    manifest, hcl = _generate([{
        "id": "1234abcd-12ab-34cd-56ef-1234567890ab", "resource_type": "aws_kms_key", "origin": "AWS_KMS",
        "description": "signing", "key_usage": "SIGN_VERIFY", "customer_master_key_spec": "RSA_2048",
        "multi_region": False, "is_enabled": False,
    }])
    assert manifest.resources_generated == 1
    assert 'key_usage = "SIGN_VERIFY"' in hcl and 'customer_master_key_spec = "RSA_2048"' in hcl
    assert "multi_region = false" in hcl and "is_enabled = false" in hcl
    assert "enable_key_rotation" not in hcl  # not read -> left to the import


def test_kms_key_with_external_key_material_goes_to_review():
    manifest, _ = _generate([{"id": "k-ext", "resource_type": "aws_kms_key", "origin": "EXTERNAL"}])
    assert manifest.resources_generated == 0 and manifest.resources_review_required == 1


def test_sqs_with_a_kms_key_never_sets_sqs_managed_sse():
    """The provider rejects sqs_managed_sse_enabled next to kms_master_key_id."""
    _, hcl = _generate([{
        "id": "https://sqs.us-east-1.amazonaws.com/123456789012/enc", "resource_type": "aws_sqs_queue",
        "name": "enc", "fifo_queue": False, "content_based_deduplication": None, "sqs_managed_sse_enabled": False,
        "kms_master_key_id": "alias/app", "kms_data_key_reuse_period_seconds": 600,
    }])
    assert 'kms_master_key_id = "alias/app"' in hcl and "kms_data_key_reuse_period_seconds = 600" in hcl
    assert "sqs_managed_sse_enabled" not in hcl
    assert "content_based_deduplication" not in hcl


def test_dynamodb_stream_and_deletion_protection_are_rendered():
    manifest, hcl = _generate([{
        "id": "events", "resource_type": "aws_dynamodb_table", "name": "events", "billing_mode": "PAY_PER_REQUEST",
        "hash_key": "pk", "hash_key_type": "S", "stream_enabled": True, "stream_view_type": "KEYS_ONLY",
        "deletion_protection_enabled": True, "table_class": "STANDARD",
    }])
    assert manifest.resources_generated == 1
    assert "stream_enabled   = true" in hcl and 'stream_view_type = "KEYS_ONLY"' in hcl
    assert "deletion_protection_enabled = true" in hcl and 'table_class = "STANDARD"' in hcl
    assert "read_capacity" not in hcl


def test_dynamodb_range_key_without_its_type_goes_to_review():
    manifest, _ = _generate([{"id": "t", "resource_type": "aws_dynamodb_table", "name": "t",
                              "hash_key": "pk", "hash_key_type": "S", "range_key": "sk"}])
    assert manifest.resources_generated == 0 and manifest.resources_review_required == 1


def test_internet_and_nat_gateways_reference_their_vpc_subnet_and_eip():
    manifest, hcl = _generate([
        {"id": "vpc-1", "resource_type": "aws_vpc", "cidr_block": "10.0.0.0/16"},
        {"id": "subnet-1", "resource_type": "aws_subnet", "vpc_id": "vpc-1", "cidr_block": "10.0.1.0/24",
         "availability_zone": "us-east-1a"},
        {"id": "igw-1", "resource_type": "aws_internet_gateway", "vpc_id": "vpc-1", "tags": [{"Key": "Name", "Value": "main"}]},
        {"id": "nat-1", "resource_type": "aws_nat_gateway", "subnet_id": "subnet-1", "allocation_id": "eipalloc-1",
         "connectivity_type": "public"},
    ])
    assert 'resource "aws_internet_gateway"' in hcl and 'resource "aws_nat_gateway"' in hcl
    assert "vpc_id = aws_vpc." in hcl and "subnet_id = aws_subnet." in hcl
    assert 'allocation_id = "eipalloc-1"' in hcl and '"Name" = "main"' in hcl


def test_public_nat_without_connectivity_type_or_eip_goes_to_review():
    manifest, _ = _generate([{"id": "nat-2", "resource_type": "aws_nat_gateway", "subnet_id": "subnet-9",
                              "connectivity_type": None}])
    assert manifest.resources_generated == 0 and manifest.resources_review_required == 1


@mock_aws
def test_kms_force_new_fields_aws_did_not_return_stay_unknown_and_are_not_rendered():
    """key_usage / multi_region force a new key: a guessed default could replace it.
    moto omits them unless given at creation, which stands in for a partial response."""
    key_id = boto3.client("kms", region_name=REGION).create_key()["KeyMetadata"]["KeyId"]
    [key] = [k for k in _scanner().scan_kms_keys() if k["id"] == key_id]
    assert key["key_usage"] is None and key["multi_region"] is None
    _, hcl = _generate([key])
    assert "key_usage" not in hcl and "multi_region" not in hcl
