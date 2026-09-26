"""AWS Resource Explorer inventory - read-only, account-wide, all regions.

Answers "what exists, and where?" in one query instead of guessing a region.
It does NOT replace AWSScanner: Resource Explorer only returns type/region/ARN
and tags, not the configuration the Composer needs to write HCL. Discovery uses
this to pick (or sanity-check) the region, then AWSScanner reads the details.

Safety (CLAUDE.md rule #3): only the calls in READ_ONLY_OPERATIONS are ever
made. Resource Explorer must already be turned on (an index, ideally an
aggregator index, plus a default view) - creating those modifies the account,
so TerraAgent never does it; it reports "not available" and discovery falls
back to the requested region instead.
"""

import logging
from collections import Counter
from typing import Any, Dict, List, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger("terraagent.resource_explorer")

READ_ONLY_OPERATIONS = ("list_indexes", "search")

# Resource Explorer "service:type" -> the ScanRequest.resource_filters group
# AWSScanner can actually turn into Terraform today.
SUPPORTED_TYPES: Dict[str, str] = {
    "ec2:instance": "EC2",
    "ec2:vpc": "VPC",
    "ec2:subnet": "VPC",
    "ec2:route-table": "VPC",
    "ec2:internet-gateway": "VPC",
    "ec2:natgateway": "VPC",
    "ec2:security-group": "SG",
    "elasticloadbalancing:loadbalancer/app": "ELB",
    "elasticloadbalancing:loadbalancer/net": "ELB",
    "s3:bucket": "S3",
    "rds:db": "RDS",
    "dynamodb:table": "DYNAMODB",
    "kms:key": "KMS",
    "sqs:queue": "SQS",
    "sns:topic": "SNS",
    "iam:role": "IAM",
}

# Resources Resource Explorer reports without a real region (IAM etc.). They
# are scanned from any region, so they must never decide which region to pick.
GLOBAL_REGIONS = {"global", "", "aws-global"}

MAX_RESOURCES = 1000  # Search caps a single query at 1000 results
MAX_RETURNED_ROWS = 500  # rows kept in job state for the UI


def _config() -> Config:
    return Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=5, read_timeout=20)


def _name_from(resource: Dict[str, Any]) -> Optional[str]:
    for prop in resource.get("Properties") or []:
        if prop.get("Name") == "tags":
            for tag in prop.get("Data") or []:
                if isinstance(tag, dict) and tag.get("Key") == "Name":
                    return tag.get("Value")
    arn = resource.get("Arn") or ""
    tail = arn.split(":")[-1]
    return tail.split("/")[-1] or None


def unavailable(reason: str) -> Dict[str, Any]:
    return {"available": False, "reason": reason}


class ResourceExplorerInventory:
    def __init__(
        self,
        access_key: str,
        secret_key: str,
        region: str,
        session_token: Optional[str] = None,
        endpoint_url: Optional[str] = None,
    ):
        self.session = boto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
        )
        self.region = region
        self.endpoint_url = endpoint_url

    def _client(self, region: str):
        return self.session.client(
            "resource-explorer-2", region_name=region, config=_config(), endpoint_url=self.endpoint_url
        )

    def _find_index(self) -> Optional[Dict[str, Any]]:
        """The aggregator index if there is one (it sees every region),
        otherwise any local index - in which case results cover that region only."""
        client = self._client(self.region)
        indexes: List[Dict[str, Any]] = []
        token = None
        while True:
            kwargs: Dict[str, Any] = {"MaxResults": 100}
            if token:
                kwargs["NextToken"] = token
            resp = client.list_indexes(**kwargs)
            indexes.extend(resp.get("Indexes") or [])
            token = resp.get("NextToken")
            if not token:
                break
        aggregator = next((i for i in indexes if i.get("Type") == "AGGREGATOR"), None)
        return aggregator or (indexes[0] if indexes else None)

    def collect(self, filters: Optional[List[str]] = None) -> Dict[str, Any]:
        filters = [f.upper() for f in (filters or list(set(SUPPORTED_TYPES.values())))]
        try:
            index = self._find_index()
            if not index:
                return unavailable(
                    f"Resource Explorer is not turned on in this account (no index found from {self.region})."
                )
            index_region = index.get("Region") or self.region
            aggregated = index.get("Type") == "AGGREGATOR"

            client = self._client(index_region)
            rows: List[Dict[str, Any]] = []
            total: Optional[int] = None
            complete = True
            token = None
            while len(rows) < MAX_RESOURCES:
                kwargs: Dict[str, Any] = {"QueryString": "", "MaxResults": min(1000, MAX_RESOURCES - len(rows))}
                if token:
                    kwargs["NextToken"] = token
                resp = client.search(**kwargs)
                if total is None:
                    count = resp.get("Count") or {}
                    total = count.get("TotalResources")
                    complete = bool(count.get("Complete", True))
                rows.extend(resp.get("Resources") or [])
                token = resp.get("NextToken")
                if not token:
                    break
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("AccessDeniedException", "UnauthorizedException", "AccessDenied"):
                return unavailable(
                    "Missing read permissions for Resource Explorer "
                    "(resource-explorer-2:ListIndexes and resource-explorer-2:Search)."
                )
            if code in ("ResourceNotFoundException",):
                return unavailable("Resource Explorer has no default view in its index region, so it can't be searched.")
            return unavailable(f"Resource Explorer query failed ({code or 'error'}).")
        except (BotoCoreError, ValueError) as e:
            return unavailable(f"Resource Explorer could not be reached ({type(e).__name__}).")

        by_region: Counter = Counter()
        by_type: Counter = Counter()
        supported_by_region: Counter = Counter()
        resources: List[Dict[str, Any]] = []
        supported_total = 0
        for r in rows:
            rtype = r.get("ResourceType") or "unknown"
            region = r.get("Region") or "global"
            group = SUPPORTED_TYPES.get(rtype)
            supported = bool(group and group in filters)
            by_region[region] += 1
            by_type[rtype] += 1
            if supported:
                supported_total += 1
                if region not in GLOBAL_REGIONS:
                    supported_by_region[region] += 1
            if len(resources) < MAX_RETURNED_ROWS:
                resources.append({
                    "arn": r.get("Arn"),
                    "type": rtype,
                    "region": region,
                    "name": _name_from(r),
                    "supported": supported,
                })

        suggested = supported_by_region.most_common(1)[0][0] if supported_by_region else None
        return {
            "available": True,
            "aggregated": aggregated,
            "index_region": index_region,
            "total": len(rows) if total is None else total,
            "truncated": (not complete) or len(rows) >= MAX_RESOURCES,
            "returned": len(rows),
            "supported_total": supported_total,
            "unsupported_total": len(rows) - supported_total,
            "by_region": dict(by_region.most_common()),
            "by_type": dict(by_type.most_common()),
            "supported_by_region": dict(supported_by_region.most_common()),
            "suggested_region": suggested,
            "resources": resources,
        }
