"""Shared Terraform resource-label naming, used by both terraform_composer
(generating resources.tf) and documentation_agent (generating import_plan.md
terraform import commands) - they must derive the exact same label for the
same resource, or the generated import commands won't match the generated
HCL's actual resource addresses."""

import hashlib
import re


def unique_clean_name(name: str, resource_id: str) -> str:
    """A human name alone is not a safe Terraform resource label: AWS
    auto-creates a security group literally named "default" in every VPC, so
    scanning more than one VPC (the common case for any real account)
    produces multiple aws_security_group resources that all want the label
    "default" - a real `terraform validate` "duplicate resource" failure,
    reproduced via a LocalStack integration test scanning two VPCs. Always
    suffixing with a short hash of the resource's actual unique ID guarantees
    no collision regardless of how many resources share a human-assigned
    name."""
    base = re.sub(r"[^a-zA-Z0-9_]", "_", (name or resource_id)).strip("_").lower()
    suffix = hashlib.md5(resource_id.encode()).hexdigest()[:8]
    return f"{base}_{suffix}" if base else suffix
