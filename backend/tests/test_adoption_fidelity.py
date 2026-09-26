"""Adoption code renders exactly what is live - tags, rules, routes - with no
hardening and no defaults, and discovered text can never inject HCL."""

from tools.hcl_generator import HCLGenerator
from tools.hcl_render import hcl_str, route_lines, security_group_rules, tags_block


def _generate(resources):
    gen = HCLGenerator(job_id="job-fidelity", region="us-east-1", engine_name="terraform", engine_version="1.8.0")
    files, manifest = gen.generate_project(resources=resources, classification_results={},
                                           adoption_plan={}, dependency_graph={})
    return files, manifest, "\n".join(v for k, v in files.items() if k.endswith(".tf"))


def test_hcl_str_escapes_everything_that_could_break_out():
    evil = 'x"\n}\nresource "aws_iam_user" "pwn" {\n  name = "${file("/etc/passwd")}" %{ if true }'
    rendered = hcl_str(evil)
    assert rendered.startswith('"') and rendered.endswith('"')
    inner = rendered[1:-1]
    # every quote inside is escaped, no raw newline, no live template sequence
    assert '"' not in inner.replace('\\"', "")
    assert "\n" not in inner
    assert "${" not in inner.replace("$${", "") and "%{" not in inner.replace("%%{", "")


def test_injection_through_a_name_tag_stays_a_string():
    _, _, hcl = _generate([{
        "id": "vpc-1", "resource_type": "aws_vpc", "cidr_block": "10.0.0.0/16",
        "name": 'prod"\n}\nresource "aws_iam_user" "pwn" {',
        "tags": [{"Key": "Name", "Value": 'prod"\n}\nresource "aws_iam_user" "pwn" {'}],
    }])
    assert 'resource "aws_iam_user"' not in hcl


def test_tags_are_copied_exactly_and_aws_tags_skipped():
    block = tags_block([{"Key": "team", "Value": "payments"}, {"Key": "Name", "Value": "web"},
                        {"Key": "aws:cloudformation:stack-name", "Value": "x"}])
    assert '"Name" = "web"' in block and '"team" = "payments"' in block and "aws:" not in block
    assert tags_block([]) == ""


def test_no_default_tags_and_no_hardening_in_adoption_code():
    files, manifest, hcl = _generate([
        {"id": "my-bucket", "resource_type": "aws_s3_bucket", "name": "my-bucket"},
        {"id": "i-1", "resource_type": "aws_instance", "name": "web", "ami": "ami-1", "instance_type": "t3.micro"},
        {"id": "db-1", "resource_type": "aws_db_instance", "name": "db-1", "engine": "postgres",
         "instance_class": "db.t3.micro", "allocated_storage": 20},
    ])
    assert "default_tags" not in files["providers.tf"]
    for hardening in ("server_side_encryption", "public_access_block", "http_tokens", "root_block_device",
                      "encrypted", "skip_final_snapshot"):
        assert hardening not in hcl, hardening
    assert "tags" not in hcl  # none of them has live tags
    assert manifest.resources_generated == 3


def test_instances_are_managed_when_the_ami_is_discovered():
    _, manifest, hcl = _generate([{"id": "i-1", "resource_type": "aws_instance", "name": "web",
                                   "ami": "ami-0abc", "instance_type": "t3.micro"}])
    assert manifest.resources_generated == 1 and 'ami           = "ami-0abc"' in hcl


def test_security_group_keeps_every_rule_source():
    perms = [{
        "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
        "IpRanges": [{"CidrIp": "10.0.0.0/8", "Description": "vpn"}, {"CidrIp": "0.0.0.0/0"}],
        "Ipv6Ranges": [{"CidrIpv6": "::/0"}],
        "PrefixListIds": [{"PrefixListId": "pl-123"}],
        "UserIdGroupPairs": [{"GroupId": "sg-other"}, {"GroupId": "sg-self"}],
    }]
    rules = security_group_rules(perms, "sg-self", lambda x: f'"{x}"')
    text = "\n".join("\n".join(r) for r in rules)
    for expected in ('"10.0.0.0/8"', '"0.0.0.0/0"', '"::/0"', '"pl-123"', '"sg-other"', "self = true",
                     'description = "vpn"'):
        assert expected in text, expected
    assert len(rules) == 2  # the "vpn" range has its own description


def test_security_group_without_description_goes_to_review():
    _, manifest, _ = _generate([{"id": "sg-1", "resource_type": "aws_security_group", "name": "web"}])
    assert manifest.resources_generated == 0 and manifest.resources_review_required == 1


def test_route_lines():
    assert route_lines({"destination_cidr_block": "10.0.0.0/16", "gateway_id": "local"}) is None
    assert route_lines({"destination_cidr_block": "0.0.0.0/0", "gateway_id": "igw-1"}) == [
        'cidr_block = "0.0.0.0/0"', 'gateway_id = "igw-1"']
    assert route_lines({"destination_cidr_block": "1.0.0.0/8", "gateway_id": "vpce-1"})[1].startswith("vpc_endpoint_id")
    assert route_lines({"destination_ipv6_cidr_block": "::/0", "egress_only_internet_gateway_id": "eigw-1"}) == [
        'ipv6_cidr_block = "::/0"', 'egress_only_gateway_id = "eigw-1"']
    assert route_lines({"destination_cidr_block": "0.0.0.0/0"}) == []  # no target: can't represent
    assert route_lines({"destination_cidr_block": "10.1.0.0/16", "gateway_id": "vgw-1",
                        "origin": "EnableVgwRoutePropagation"}) is None


def test_unrepresentable_route_sends_the_table_to_review():
    _, manifest, _ = _generate([{"id": "rtb-1", "resource_type": "aws_route_table", "vpc_id": "vpc-1",
                                 "routes": [{"destination_cidr_block": "0.0.0.0/0"}]}])
    assert manifest.resources_review_required == 1


def test_route_table_associations_are_generated_and_imported():
    from tools.import_blocks import import_targets
    from tools.naming import unique_clean_name

    rt = {"id": "rtb-1", "resource_type": "aws_route_table", "name": "public", "vpc_id": "vpc-1",
          "routes": [{"destination_cidr_block": "0.0.0.0/0", "gateway_id": "igw-1"}],
          "associated_subnets": ["subnet-b", "subnet-a"]}
    subnet = {"id": "subnet-a", "resource_type": "aws_subnet", "name": "a", "vpc_id": "vpc-1",
              "cidr_block": "10.0.1.0/24", "map_public_ip_on_launch": True}
    files, manifest, hcl = _generate([rt, subnet])
    rt_name = unique_clean_name("public", "rtb-1")

    assert hcl.count('resource "aws_route_table_association"') == 2
    targets = import_targets(files["imports.tf"])
    # sorted subnets -> deterministic addresses; the managed subnet is referenced, the other is literal
    assert targets[f"aws_route_table_association.{rt_name}_assoc_0"] == "subnet-a/rtb-1"
    assert targets[f"aws_route_table_association.{rt_name}_assoc_1"] == "subnet-b/rtb-1"
    assert 'subnet_id      = "subnet-b"' in hcl
    assert "subnet associations are left unmanaged" not in " ".join(manifest.warnings)


def test_wave_pr_carries_route_table_associations():
    from services.github_client import _extract_wave_files
    from tools.import_blocks import import_targets

    rt = {"id": "rtb-1", "resource_type": "aws_route_table", "name": "public", "vpc_id": "vpc-1",
          "routes": [], "associated_subnets": ["subnet-a"]}
    files, _, _ = _generate([rt])
    wave = _extract_wave_files(files, ["rtb-1"], {"rtb-1": rt})
    stack = "\n".join(v for k, v in wave.items() if k != "imports.tf")
    assert 'resource "aws_route_table_association"' in stack
    assert len(import_targets(wave["imports.tf"])) == 2


def test_dynamodb_fidelity_with_range_key_and_provisioned_capacity():
    table = {
        "id": "app-orders", "resource_type": "aws_dynamodb_table", "name": "app-orders",
        "billing_mode": "PROVISIONED", "hash_key": "order_id", "hash_key_type": "S",
        "range_key": "created_at", "range_key_type": "N",
        "read_capacity": 10, "write_capacity": 5, "has_indexes": False,
        "tags": [{"Key": "Environment", "Value": "prod"}]
    }
    files, manifest, hcl = _generate([table])
    assert manifest.resources_generated == 1
    assert 'billing_mode = "PROVISIONED"' in hcl
    assert 'hash_key     = "order_id"' in hcl
    assert 'range_key    = "created_at"' in hcl
    assert 'read_capacity  = 10' in hcl
    assert 'write_capacity = 5' in hcl
    assert '"Environment" = "prod"' in hcl


def test_dynamodb_with_indexes_goes_to_review():
    table = {
        "id": "app-orders-indexed", "resource_type": "aws_dynamodb_table", "name": "app-orders-indexed",
        "billing_mode": "PAY_PER_REQUEST", "hash_key": "order_id", "hash_key_type": "S",
        "has_indexes": True,
    }
    _, manifest, _ = _generate([table])
    assert manifest.resources_generated == 0
    assert manifest.resources_review_required == 1


def test_sqs_fidelity_with_all_attributes():
    queue = {
        "id": "https://sqs.us-east-1.amazonaws.com/123456789012/events.fifo",
        "resource_type": "aws_sqs_queue", "name": "events.fifo",
        "visibility_timeout_seconds": 60, "message_retention_seconds": 86400,
        "delay_seconds": 5, "fifo_queue": True, "content_based_deduplication": True,
        "sqs_managed_sse_enabled": True, "redrive_policy": '{"maxReceiveCount": 3}',
    }
    files, manifest, hcl = _generate([queue])
    assert manifest.resources_generated == 1
    assert 'visibility_timeout_seconds = 60' in hcl
    assert 'message_retention_seconds = 86400' in hcl
    assert 'delay_seconds = 5' in hcl
    assert 'fifo_queue = true' in hcl
    assert 'content_based_deduplication = true' in hcl
    assert 'sqs_managed_sse_enabled = true' in hcl
    assert 'redrive_policy = jsonencode(' in hcl


def test_sns_fidelity_with_display_name_and_fifo():
    topic = {
        "id": "arn:aws:sns:us-east-1:123456789012:alerts.fifo",
        "resource_type": "aws_sns_topic", "name": "alerts.fifo",
        "display_name": "Production Alerts", "fifo_topic": True,
    }
    files, manifest, hcl = _generate([topic])
    assert manifest.resources_generated == 1
    assert 'display_name = "Production Alerts"' in hcl
    assert 'fifo_topic = true' in hcl

