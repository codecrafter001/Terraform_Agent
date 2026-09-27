"""Read-only discovery for the service types beyond the core VPC/EC2/S3/RDS/IAM
scans in tools/aws_scanner.py (a mixin of AWSScanner):

- aws_internet_gateway, aws_nat_gateway (EC2)
- aws_lb (ELBv2)
- aws_dynamodb_table (DynamoDB)
- aws_kms_key (KMS)
- aws_sqs_queue (SQS)
- aws_sns_topic (SNS)

Only Describe*/Get*/List* calls (tests/test_cloud_discovery.py records every
operation). Each record carries exactly what the adoption template needs for a
zero-change plan. A read that fails goes through self._failed - the scan is
then INCOMPLETE - and the value is left out (unknown), never guessed: an empty
tag set or a `false` default in place of an unread value would make the
adoption plan change the live resource.
"""

from typing import Any, Callable, Dict, List, Optional

from botocore.exceptions import BotoCoreError, ClientError

# NAT gateways in these states are gone or going; there is nothing to adopt.
_NAT_GONE_STATES = {"deleted", "deleting", "failed"}

# elbv2 DescribeLoadBalancerAttributes key -> (aws_lb argument, kind, load balancer types it applies to).
# Only arguments whose provider default could differ from the live value;
# omitting one of these would make the plan reset it to the default.
_LB_ATTRIBUTES: Dict[str, tuple] = {
    "deletion_protection.enabled": ("enable_deletion_protection", "bool", ("application", "network", "gateway")),
    "idle_timeout.timeout_seconds": ("idle_timeout", "int", ("application",)),
    "routing.http2.enabled": ("enable_http2", "bool", ("application",)),
    "routing.http.drop_invalid_header_fields.enabled": ("drop_invalid_header_fields", "bool", ("application",)),
    "load_balancing.cross_zone.enabled": ("enable_cross_zone_load_balancing", "bool", ("network", "gateway")),
}

# KMS key states in which GetKeyRotationStatus / ListResourceTags are refused.
_KMS_UNREADABLE_STATES = {"PendingDeletion", "PendingImport", "Unavailable", "PendingReplicaDeletion"}


class ExtraServiceScans:
    """Mixin for AWSScanner. Relies on its _client, _failed and _get_tag."""

    _client: Callable[[str], Any]
    _failed: Callable[[str, Exception], None]
    _get_tag: Callable[[Any, str, str], str]

    def _read(self, scope: str, call: Callable[[], Any]) -> Optional[Any]:
        """One optional read: its result, or None after recording the failure."""
        try:
            return call()
        except (ClientError, BotoCoreError) as e:
            self._failed(scope, e)
            return None

    def scan_internet_gateways(self) -> List[Dict[str, Any]]:
        """Internet gateways; is_default marks the one attached to the default VPC."""
        results: List[Dict[str, Any]] = []
        try:
            ec2 = self._client("ec2")
            default_vpcs = self._read(
                "default VPC lookup for internet gateways",
                lambda: {v["VpcId"] for v in ec2.describe_vpcs(
                    Filters=[{"Name": "isDefault", "Values": ["true"]}]).get("Vpcs", [])},
            )
            for page in ec2.get_paginator("describe_internet_gateways").paginate():
                for igw in page.get("InternetGateways", []):
                    igw_id = igw["InternetGatewayId"]
                    attachments = igw.get("Attachments", [])
                    vpc_id = attachments[0].get("VpcId") if attachments else None
                    results.append({
                        "resource_type": "aws_internet_gateway",
                        "id": igw_id,
                        "name": self._get_tag(igw.get("Tags", []), "Name", igw_id),
                        "vpc_id": vpc_id,
                        # None when the default-VPC lookup failed (already recorded).
                        "is_default": bool(vpc_id and vpc_id in default_vpcs) if default_vpcs is not None else None,
                        "tags": igw.get("Tags", []),
                    })
        except (ClientError, BotoCoreError) as e:
            self._failed("Internet Gateways", e)
        return results

    def scan_nat_gateways(self) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        try:
            ec2 = self._client("ec2")
            for page in ec2.get_paginator("describe_nat_gateways").paginate():
                for nat in page.get("NatGateways", []):
                    if nat.get("State") in _NAT_GONE_STATES:
                        continue
                    nat_id = nat["NatGatewayId"]
                    addrs = nat.get("NatGatewayAddresses", [])
                    results.append({
                        "resource_type": "aws_nat_gateway",
                        "id": nat_id,
                        "name": self._get_tag(nat.get("Tags", []), "Name", nat_id),
                        "subnet_id": nat.get("SubnetId"),
                        "vpc_id": nat.get("VpcId"),
                        "allocation_id": addrs[0].get("AllocationId") if addrs else None,
                        "connectivity_type": nat.get("ConnectivityType"),
                        "tags": nat.get("Tags", []),
                    })
        except (ClientError, BotoCoreError) as e:
            self._failed("NAT Gateways", e)
        return results

    def scan_load_balancers(self) -> List[Dict[str, Any]]:
        """ELBv2 load balancers (application / network / gateway)."""
        results: List[Dict[str, Any]] = []
        try:
            elbv2 = self._client("elbv2")
            lbs: List[Dict[str, Any]] = []
            for page in elbv2.get_paginator("describe_load_balancers").paginate():
                lbs.extend(page.get("LoadBalancers", []))

            # DescribeTags takes at most 20 ARNs per call.
            tags_by_arn: Dict[str, List[Dict[str, str]]] = {}
            for i in range(0, len(lbs), 20):
                arns = [lb["LoadBalancerArn"] for lb in lbs[i:i + 20]]
                described = self._read(
                    "load balancer tags",
                    lambda arns=arns: elbv2.describe_tags(ResourceArns=arns).get("TagDescriptions", []),
                )
                for desc in described or []:
                    tags_by_arn[desc["ResourceArn"]] = desc.get("Tags", [])

            for lb in lbs:
                arn = lb["LoadBalancerArn"]
                lb_type = lb.get("Type")
                record: Dict[str, Any] = {
                    "resource_type": "aws_lb",
                    "id": arn,
                    "arn": arn,
                    "name": lb.get("LoadBalancerName"),
                    "scheme": lb.get("Scheme"),
                    "load_balancer_type": lb_type,
                    "subnets": [az["SubnetId"] for az in lb.get("AvailabilityZones", []) if az.get("SubnetId")],
                    "security_groups": lb.get("SecurityGroups", []),
                    "vpc_id": lb.get("VpcId"),
                }
                if arn in tags_by_arn:
                    record["tags"] = tags_by_arn[arn]
                attributes = self._read(
                    f"attributes of load balancer {record['name']}",
                    lambda arn=arn: elbv2.describe_load_balancer_attributes(LoadBalancerArn=arn).get("Attributes", []),
                )
                for attr in attributes or []:
                    spec = _LB_ATTRIBUTES.get(attr.get("Key", ""))
                    if not spec or lb_type not in spec[2]:
                        continue
                    name, kind, _ = spec
                    value = str(attr.get("Value", ""))
                    if kind == "bool" and value in ("true", "false"):
                        record[name] = value == "true"
                    elif kind == "int" and value.isdigit():
                        record[name] = int(value)
                results.append(record)
        except (ClientError, BotoCoreError) as e:
            self._failed("Load Balancers", e)
        return results

    def scan_dynamodb_tables(self) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        try:
            ddb = self._client("dynamodb")
            table_names: List[str] = []
            for page in ddb.get_paginator("list_tables").paginate():
                table_names.extend(page.get("TableNames", []))

            for tbl_name in table_names:
                desc = self._read(f"DynamoDB table {tbl_name}",
                                  lambda n=tbl_name: ddb.describe_table(TableName=n).get("Table", {}))
                if desc is None:
                    continue
                keys = {k.get("KeyType"): k.get("AttributeName") for k in desc.get("KeySchema", [])}
                attr_types = {a.get("AttributeName"): a.get("AttributeType") for a in desc.get("AttributeDefinitions", [])}
                hash_key, range_key = keys.get("HASH"), keys.get("RANGE")
                # A table created with provisioned capacity has no BillingModeSummary.
                billing_mode = (desc.get("BillingModeSummary") or {}).get("BillingMode", "PROVISIONED")
                throughput = desc.get("ProvisionedThroughput") or {}
                stream = desc.get("StreamSpecification") or {}
                arn = desc.get("TableArn")

                record: Dict[str, Any] = {
                    "resource_type": "aws_dynamodb_table",
                    "id": tbl_name,
                    "name": tbl_name,
                    "arn": arn,
                    "billing_mode": billing_mode,
                    "hash_key": hash_key,
                    "hash_key_type": attr_types.get(hash_key) if hash_key else None,
                    "range_key": range_key,
                    "range_key_type": attr_types.get(range_key) if range_key else None,
                    "read_capacity": throughput.get("ReadCapacityUnits") if billing_mode == "PROVISIONED" else None,
                    "write_capacity": throughput.get("WriteCapacityUnits") if billing_mode == "PROVISIONED" else None,
                    "has_indexes": bool(desc.get("GlobalSecondaryIndexes") or desc.get("LocalSecondaryIndexes")),
                    # stream_enabled and deletion_protection_enabled default to false in
                    # the provider, so leaving a live `true` out would switch it off.
                    "stream_enabled": bool(stream.get("StreamEnabled")),
                    "stream_view_type": stream.get("StreamViewType") if stream.get("StreamEnabled") else None,
                    "deletion_protection_enabled": desc.get("DeletionProtectionEnabled"),
                    "table_class": (desc.get("TableClassSummary") or {}).get("TableClass"),
                }
                if arn:
                    tags = self._read(f"tags of DynamoDB table {tbl_name}",
                                      lambda a=arn: ddb.list_tags_of_resource(ResourceArn=a).get("Tags", []))
                    if tags is not None:
                        record["tags"] = tags
                results.append(record)
        except (ClientError, BotoCoreError) as e:
            self._failed("DynamoDB tables", e)
        return results

    def scan_kms_keys(self) -> List[Dict[str, Any]]:
        """Every key, AWS-managed ones included - the classifier excludes those."""
        results: List[Dict[str, Any]] = []
        try:
            kms = self._client("kms")
            key_ids: List[str] = []
            for page in kms.get_paginator("list_keys").paginate():
                key_ids.extend(k["KeyId"] for k in page.get("Keys", []) if k.get("KeyId"))

            for key_id in key_ids:
                meta = self._read(f"KMS key {key_id}",
                                  lambda k=key_id: kms.describe_key(KeyId=k).get("KeyMetadata", {}))
                if meta is None:
                    continue
                key_manager = meta.get("KeyManager")
                key_state = meta.get("KeyState")
                key_spec = meta.get("KeySpec") or meta.get("CustomerMasterKeySpec")
                record: Dict[str, Any] = {
                    "resource_type": "aws_kms_key",
                    "id": key_id,
                    "key_id": key_id,
                    "arn": meta.get("Arn"),
                    "description": meta.get("Description"),
                    "key_manager": key_manager,
                    "key_state": key_state,
                    "origin": meta.get("Origin"),
                    # key_usage, customer_master_key_spec and multi_region force a new
                    # key when they differ - they must be the live values.
                    "key_usage": meta.get("KeyUsage"),
                    "customer_master_key_spec": key_spec,
                    "multi_region": meta.get("MultiRegion"),
                    "is_enabled": {"Enabled": True, "Disabled": False}.get(key_state or ""),
                }
                if key_manager == "CUSTOMER" and key_state not in _KMS_UNREADABLE_STATES:
                    # Only symmetric encryption keys can rotate automatically.
                    if key_spec == "SYMMETRIC_DEFAULT":
                        status = self._read(f"rotation status of KMS key {key_id}",
                                            lambda k=key_id: kms.get_key_rotation_status(KeyId=k))
                        if status is not None:
                            record["enable_key_rotation"] = bool(status.get("KeyRotationEnabled"))
                    tags = self._read(f"tags of KMS key {key_id}",
                                      lambda k=key_id: kms.list_resource_tags(KeyId=k).get("Tags", []))
                    if tags is not None:
                        # KMS spells them TagKey/TagValue.
                        record["tags"] = [{"Key": t.get("TagKey"), "Value": t.get("TagValue")} for t in tags]
                results.append(record)
        except (ClientError, BotoCoreError) as e:
            self._failed("KMS keys", e)
        return results

    def scan_sqs_queues(self) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        try:
            sqs = self._client("sqs")
            queue_urls: List[str] = []
            for page in sqs.get_paginator("list_queues").paginate():
                queue_urls.extend(page.get("QueueUrls", []))

            for url in queue_urls:
                attrs = self._read(f"SQS queue {url}", lambda u=url: sqs.get_queue_attributes(
                    QueueUrl=u, AttributeNames=["All"]).get("Attributes", {}))
                if attrs is None:
                    continue

                def number(key: str) -> Optional[int]:
                    value = attrs.get(key)
                    return int(value) if value is not None and str(value).isdigit() else None

                def flag(key: str) -> Optional[bool]:
                    value = attrs.get(key)
                    return None if value is None else str(value).lower() == "true"

                fifo = url.endswith(".fifo")
                record: Dict[str, Any] = {
                    "resource_type": "aws_sqs_queue",
                    "id": url,
                    "url": url,
                    "name": url.rsplit("/", 1)[-1],
                    "arn": attrs.get("QueueArn"),
                    "visibility_timeout_seconds": number("VisibilityTimeout"),
                    "message_retention_seconds": number("MessageRetentionPeriod"),
                    "delay_seconds": number("DelaySeconds"),
                    "max_message_size": number("MaximumMessageSize"),
                    "receive_wait_time_seconds": number("ReceiveMessageWaitTimeSeconds"),
                    "fifo_queue": fifo,
                    "content_based_deduplication": flag("ContentBasedDeduplication") if fifo else None,
                    "sqs_managed_sse_enabled": flag("SqsManagedSseEnabled"),
                    "kms_master_key_id": attrs.get("KmsMasterKeyId"),
                    "kms_data_key_reuse_period_seconds": number("KmsDataKeyReusePeriodSeconds"),
                    "redrive_policy": attrs.get("RedrivePolicy"),
                }
                tags = self._read(f"tags of SQS queue {record['name']}",
                                  lambda u=url: sqs.list_queue_tags(QueueUrl=u).get("Tags", {}))
                if tags is not None:
                    record["tags"] = [{"Key": k, "Value": v} for k, v in tags.items()]
                results.append(record)
        except (ClientError, BotoCoreError) as e:
            self._failed("SQS queues", e)
        return results

    def scan_sns_topics(self) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        try:
            sns = self._client("sns")
            topic_arns: List[str] = []
            for page in sns.get_paginator("list_topics").paginate():
                topic_arns.extend(t["TopicArn"] for t in page.get("Topics", []) if t.get("TopicArn"))

            for arn in topic_arns:
                attrs = self._read(f"SNS topic {arn}",
                                   lambda a=arn: sns.get_topic_attributes(TopicArn=a).get("Attributes", {}))
                if attrs is None:
                    continue
                fifo = str(attrs.get("FifoTopic", "")).lower() == "true"
                record: Dict[str, Any] = {
                    "resource_type": "aws_sns_topic",
                    "id": arn,
                    "arn": arn,
                    "name": arn.rsplit(":", 1)[-1],
                    "display_name": attrs.get("DisplayName"),
                    "fifo_topic": fifo,
                    "content_based_deduplication": (
                        str(attrs.get("ContentBasedDeduplication", "")).lower() == "true" if fifo else None
                    ),
                    "kms_master_key_id": attrs.get("KmsMasterKeyId"),
                }
                tags = self._read(f"tags of SNS topic {record['name']}",
                                  lambda a=arn: sns.list_tags_for_resource(ResourceArn=a).get("Tags", []))
                if tags is not None:
                    record["tags"] = tags
                results.append(record)
        except (ClientError, BotoCoreError) as e:
            self._failed("SNS topics", e)
        return results
