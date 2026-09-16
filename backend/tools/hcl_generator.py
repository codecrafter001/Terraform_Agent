"""Deterministic HCL code generator for TerraAgent P2 engine.

Transforms structured AWS resource discovery data and adoption plans into clean,
modular, dependency-aware, and reproducible Terraform / OpenTofu HCL code.

Strictly adheres to:
1. Zero fake defaults (no invented CIDRs, AMIs, or instance types). Missing required
   attributes immediately mark the resource for manual review with an explicit reason.
2. Real dependency preservation (replaces raw ID strings with Terraform references like
   `aws_vpc.main.id` or `data.aws_vpc.main.id` when targets are part of the synthesis).
3. Authoritative adoption plan consumption (safe_to_import -> resource, use_data_source -> data,
   do_not_manage -> excluded, review_required/unsupported -> excluded with explicit reporting).
"""

import json
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from models.manifest import GenerationManifest, UnresolvedAttribute
from tools.naming import unique_clean_name

logger = logging.getLogger("terraagent.hcl_generator")


class HCLGenerator:
    """Deterministic, dependency-aware HCL synthesizer."""

    def __init__(
        self,
        job_id: str,
        region: str = "us-east-1",
        engine_name: str = "terraform",
        engine_version: Optional[str] = None
    ):
        self.job_id = job_id
        self.region = region
        self.engine_name = engine_name
        self.engine_version = engine_version

    def generate_project(
        self,
        resources: List[Dict[str, Any]],
        classification_results: Dict[str, Any],
        adoption_plan: Dict[str, Any],
        dependency_graph: Dict[str, Any]
    ) -> Tuple[Dict[str, str], GenerationManifest]:
        """Synthesizes modular HCL files and a comprehensive generation manifest."""
        # 1. Build lookup tables for adoption outcomes and clean names
        classifications = classification_results.get("classifications", []) or []
        classification_action_map = {
            c["resource_id"]: c.get("recommended_action", "import")
            for c in classifications if c.get("resource_id")
        }
        classification_cat_map = {
            c["resource_id"]: c.get("category", "unmanaged")
            for c in classifications if c.get("resource_id")
        }

        # Adoption plan takes precedence if present
        plan_category_map: Dict[str, str] = {}
        for cat_plan in adoption_plan.get("categories", []):
            cat_name = cat_plan.get("category")
            for rid in cat_plan.get("resource_ids", []):
                plan_category_map[rid] = cat_name

        # Map resource_id -> (resource_type, clean_name, is_data_source)
        resource_ref_map: Dict[str, Dict[str, Any]] = {}
        for res in resources:
            r_id = res.get("id")
            if not r_id:
                continue
            r_type = res.get("resource_type", "aws_resource")
            clean_name = unique_clean_name(res.get("name", r_id), r_id)
            plan_cat = plan_category_map.get(r_id)
            action = classification_action_map.get(r_id, "import")

            is_data = (plan_cat == "use_data_source") or (action == "data_source")
            is_managed = (plan_cat == "safe_to_import") or (action == "import" and not plan_cat)

            if is_managed or is_data:
                resource_ref_map[r_id] = {
                    "resource_type": r_type,
                    "clean_name": clean_name,
                    "is_data_source": is_data,
                    "tf_address": f"data.{r_type}.{clean_name}" if is_data else f"{r_type}.{clean_name}"
                }

        # 2. Stack mapping from dependency graph
        stack_by_resource_id: Dict[str, str] = {
            rid: stack["name"]
            for stack in dependency_graph.get("stacks", []) or []
            for rid in stack.get("resource_ids", [])
        }

        # 3. Process each resource
        resource_blocks_by_stack: Dict[str, List[str]] = {}
        output_blocks_by_stack: Dict[str, List[str]] = {}

        manifest = GenerationManifest(
            job_id=self.job_id,
            engine=self.engine_name,
            engine_version=self.engine_version,
            region=self.region,
            resources_discovered=len(resources)
        )

        for res in resources:
            r_id = res.get("id")
            if not r_id:
                continue

            r_type = res.get("resource_type", "aws_resource")
            clean_name = unique_clean_name(res.get("name", r_id), r_id)
            stack_name = stack_by_resource_id.get(r_id, self._default_stack_for_type(r_type))

            action = classification_action_map.get(r_id, "import")
            category = classification_cat_map.get(r_id, "unmanaged")
            plan_cat = plan_category_map.get(r_id)

            # Determine explicit outcome
            if plan_cat == "do_not_manage" or action == "skip" or category == "managed":
                manifest.adoption_outcomes[r_id] = "do_not_manage"
                manifest.resources_skipped += 1
                continue

            if plan_cat == "use_data_source" or action == "data_source" or category == "shared":
                manifest.adoption_outcomes[r_id] = "use_data_source"
                manifest.resources_data_source += 1
                ds_block = self._compose_data_source(r_type, res, clean_name)
                resource_blocks_by_stack.setdefault(stack_name, []).append(ds_block)
                continue

            if plan_cat in ("review_required", "unsupported") or category in ("unsupported", "orphaned") or action == "manual_review":
                if plan_cat == "unsupported" or category == "unsupported":
                    manifest.adoption_outcomes[r_id] = "unsupported"
                    manifest.resources_unsupported += 1
                else:
                    manifest.adoption_outcomes[r_id] = "review_required"
                    manifest.resources_review_required += 1
                continue

            # Resource is safe to import: Generate managed block with strict validation (no fake defaults)
            manifest.adoption_outcomes[r_id] = "safe_to_import"
            hcl_block, outputs, unresolved, warnings = self._compose_managed_resource(
                r_type, res, clean_name, resource_ref_map
            )

            if unresolved:
                manifest.unresolved_attributes.extend(unresolved)
                manifest.warnings.extend(warnings)
                manifest.resources_review_required += 1
                manifest.adoption_outcomes[r_id] = "review_required"
                # If required attributes were missing, do not generate a broken or fake resource block
                continue

            if warnings:
                manifest.warnings.extend(warnings)

            manifest.resources_generated += 1
            resource_blocks_by_stack.setdefault(stack_name, []).append(hcl_block)
            if outputs:
                output_blocks_by_stack.setdefault(stack_name, []).extend(outputs)

        # 4. Generate core project files
        files: Dict[str, str] = {
            "versions.tf": self._compose_versions_tf(),
            "providers.tf": self._compose_providers_tf(),
            "variables.tf": self._compose_variables_tf(),
            "locals.tf": self._compose_locals_tf(),
        }

        # Add domain/stack files
        for stack_name, blocks in sorted(resource_blocks_by_stack.items()):
            if blocks:
                files[f"{stack_name}.tf"] = "\n\n".join(blocks)

        all_outputs = [b for blocks in output_blocks_by_stack.values() for b in blocks]
        if all_outputs:
            files["outputs.tf"] = "\n\n".join(all_outputs)

        manifest.generated_files = sorted(files.keys())
        return files, manifest

    def _compose_versions_tf(self) -> str:
        return """terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
"""

    def _compose_providers_tf(self) -> str:
        return """provider "aws" {
  region = var.aws_region
  default_tags {
    tags = local.default_tags
  }
}
"""

    def _compose_variables_tf(self) -> str:
        return f"""variable "aws_region" {{
  type        = string
  description = "Target AWS deployment region"
  default     = "{self.region}"
}}

variable "environment" {{
  type        = string
  description = "Target deployment stage"
  default     = "production"
}}
"""

    def _compose_locals_tf(self) -> str:
        return """locals {
  default_tags = {
    Environment = var.environment
    ManagedBy   = "TerraAgent"
  }
}
"""

    def _compose_data_source(self, r_type: str, res: Dict[str, Any], clean_name: str) -> str:
        r_id = res.get("id", "")
        if r_type == "aws_security_group":
            return f"""data "aws_security_group" "{clean_name}" {{
  id = "{r_id}"
}}"""
        elif r_type == "aws_vpc":
            return f"""data "aws_vpc" "{clean_name}" {{
  id = "{r_id}"
}}"""
        elif r_type == "aws_subnet":
            return f"""data "aws_subnet" "{clean_name}" {{
  id = "{r_id}"
}}"""
        return f"""data "{r_type}" "{clean_name}" {{
  id = "{r_id}"
}}"""

    def _compose_managed_resource(
        self,
        r_type: str,
        res: Dict[str, Any],
        clean_name: str,
        resource_ref_map: Dict[str, Dict[str, Any]]
    ) -> Tuple[str, List[str], List[UnresolvedAttribute], List[str]]:
        """Synthesizes resource HCL without fake defaults. Returns (hcl, outputs, unresolved, warnings)."""
        r_id = res.get("id", "")
        outputs: List[str] = []
        unresolved: List[UnresolvedAttribute] = []
        warnings: List[str] = []

        if r_type == "aws_vpc":
            cidr = res.get("cidr_block")
            if not cidr:
                unresolved.append(UnresolvedAttribute(
                    resource_id=r_id,
                    resource_type=r_type,
                    attribute_name="cidr_block",
                    reason="VPC discovery record is missing 'cidr_block'."
                ))
                return "", [], unresolved, warnings

            hcl = f"""resource "aws_vpc" "{clean_name}" {{
  cidr_block           = "{cidr}"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {{
    Name = "{res.get('name', clean_name)}"
  }}
}}"""
            outputs.append(f"""output "vpc_{clean_name}_id" {{
  value       = aws_vpc.{clean_name}.id
  description = "VPC ID for {clean_name}"
}}""")
            return hcl, outputs, [], warnings

        elif r_type == "aws_subnet":
            cidr = res.get("cidr_block")
            vpc_id = res.get("vpc_id")
            if not cidr:
                unresolved.append(UnresolvedAttribute(
                    resource_id=r_id,
                    resource_type=r_type,
                    attribute_name="cidr_block",
                    reason="Subnet discovery record is missing 'cidr_block'."
                ))
            if not vpc_id:
                unresolved.append(UnresolvedAttribute(
                    resource_id=r_id,
                    resource_type=r_type,
                    attribute_name="vpc_id",
                    reason="Subnet discovery record is missing 'vpc_id'."
                ))
            if unresolved:
                return "", [], unresolved, warnings

            # Dependency reference: use aws_vpc.<name>.id if available
            vpc_ref = f'"{vpc_id}"'
            if vpc_id in resource_ref_map:
                target = resource_ref_map[vpc_id]
                vpc_ref = f"{target['tf_address']}.id"

            az_attr = f'\n  availability_zone       = "{res.get("availability_zone")}"' if res.get("availability_zone") else ""

            hcl = f"""resource "aws_subnet" "{clean_name}" {{
  vpc_id                  = {vpc_ref}
  cidr_block              = "{cidr}"{az_attr}
  map_public_ip_on_launch = false

  tags = {{
    Name = "{res.get('name', clean_name)}"
  }}
}}"""
            outputs.append(f"""output "subnet_{clean_name}_id" {{
  value       = aws_subnet.{clean_name}.id
  description = "Subnet ID for {clean_name}"
}}""")
            return hcl, outputs, [], warnings

        elif r_type == "aws_security_group":
            vpc_id = res.get("vpc_id")
            vpc_ref_attr = ""
            if vpc_id:
                if vpc_id in resource_ref_map:
                    target = resource_ref_map[vpc_id]
                    vpc_ref_attr = f"\n  vpc_id      = {target['tf_address']}.id"
                else:
                    vpc_ref_attr = f'\n  vpc_id      = "{vpc_id}"'

            description = (res.get("description") or "Managed by TerraAgent").replace('"', "'")

            def _format_rules(block_name: str, perms: list) -> str:
                rules = []
                for perm in perms:
                    cidrs = [c.get("CidrIp") for c in perm.get("IpRanges", []) if c.get("CidrIp")]
                    if not cidrs:
                        continue
                    from_p = perm.get("FromPort") or 0
                    to_p = perm.get("ToPort") or 0
                    proto = perm.get("IpProtocol", "-1")
                    rules.append(f"""  {block_name} {{
    from_port   = {from_p}
    to_port     = {to_p}
    protocol    = "{proto}"
    cidr_blocks = [{", ".join(f'"{c}"' for c in cidrs)}]
  }}""")
                return "\n\n".join(rules)

            ingress_hcl = _format_rules("ingress", res.get("ip_permissions", []))
            egress_hcl = _format_rules("egress", res.get("ip_permissions_egress", []))

            sections = [f'  name        = "{res.get("name", clean_name)}"\n  description = "{description}"{vpc_ref_attr}']
            if ingress_hcl:
                sections.append(ingress_hcl)
            if egress_hcl:
                sections.append(egress_hcl)
            sections.append(f'  tags = {{\n    Name = "{res.get("name", clean_name)}"\n  }}')

            hcl = f'resource "aws_security_group" "{clean_name}" {{\n' + "\n\n".join(sections) + "\n}"
            outputs.append(f"""output "sg_{clean_name}_id" {{
  value       = aws_security_group.{clean_name}.id
  description = "Security Group ID for {clean_name}"
}}""")
            return hcl, outputs, [], warnings

        elif r_type == "aws_instance":
            ami = res.get("ami")
            inst_type = res.get("instance_type")
            if not ami:
                unresolved.append(UnresolvedAttribute(
                    resource_id=r_id,
                    resource_type=r_type,
                    attribute_name="ami",
                    reason="EC2 Instance discovery record is missing required 'ami'."
                ))
            if not inst_type:
                unresolved.append(UnresolvedAttribute(
                    resource_id=r_id,
                    resource_type=r_type,
                    attribute_name="instance_type",
                    reason="EC2 Instance discovery record is missing required 'instance_type'."
                ))
            if unresolved:
                return "", [], unresolved, warnings

            # Dependency references for subnet & security groups
            subnet_id = res.get("subnet_id")
            subnet_attr = ""
            if subnet_id:
                if subnet_id in resource_ref_map:
                    target = resource_ref_map[subnet_id]
                    subnet_attr = f"\n  subnet_id     = {target['tf_address']}.id"
                else:
                    subnet_attr = f'\n  subnet_id     = "{subnet_id}"'

            sg_ids = res.get("security_groups") or []
            sg_attr = ""
            if sg_ids:
                refs = []
                for sid in sg_ids:
                    if sid in resource_ref_map:
                        refs.append(f"{resource_ref_map[sid]['tf_address']}.id")
                    else:
                        refs.append(f'"{sid}"')
                sg_attr = f"\n  vpc_security_group_ids = [{', '.join(refs)}]"

            # IAM profile reference if present
            profile_arn = res.get("iam_instance_profile_arn")
            iam_attr = ""
            if profile_arn:
                role_name = profile_arn.rsplit("/", 1)[-1]
                if role_name in resource_ref_map:
                    iam_attr = f"\n  iam_instance_profile   = {resource_ref_map[role_name]['tf_address']}.name"
                else:
                    iam_attr = f'\n  iam_instance_profile   = "{role_name}"'

            hcl = f"""resource "aws_instance" "{clean_name}" {{
  ami           = "{ami}"
  instance_type = "{inst_type}"{subnet_attr}{sg_attr}{iam_attr}

  metadata_options {{
    http_endpoint = "enabled"
    http_tokens   = "required"
  }}

  root_block_device {{
    encrypted = true
  }}

  tags = {{
    Name = "{res.get('name', clean_name)}"
  }}
}}"""
            outputs.append(f"""output "instance_{clean_name}_id" {{
  value       = aws_instance.{clean_name}.id
  description = "EC2 Instance ID for {clean_name}"
}}""")
            return hcl, outputs, [], warnings

        elif r_type == "aws_route_table":
            vpc_id = res.get("vpc_id")
            if not vpc_id:
                unresolved.append(UnresolvedAttribute(
                    resource_id=r_id,
                    resource_type=r_type,
                    attribute_name="vpc_id",
                    reason="Route Table discovery record is missing 'vpc_id'."
                ))
                return "", [], unresolved, warnings

            vpc_ref = f'"{vpc_id}"'
            if vpc_id in resource_ref_map:
                target = resource_ref_map[vpc_id]
                vpc_ref = f"{target['tf_address']}.id"

            route_blocks = []
            for route in res.get("routes", []):
                cidr = route.get("destination_cidr_block")
                if not cidr:
                    continue
                route_attrs = [f'    cidr_block = "{cidr}"']
                if route.get("gateway_id"):
                    route_attrs.append(f'    gateway_id = "{route["gateway_id"]}"')
                elif route.get("nat_gateway_id"):
                    route_attrs.append(f'    nat_gateway_id = "{route["nat_gateway_id"]}"')
                route_blocks.append("  route {\n" + "\n".join(route_attrs) + "\n  }")

            body = [f"  vpc_id = {vpc_ref}"]
            if route_blocks:
                body.append("\n\n".join(route_blocks))
            body.append(f'  tags = {{\n    Name = "{res.get("name", clean_name)}"\n  }}')

            hcl_parts = [f'resource "aws_route_table" "{clean_name}" {{\n' + "\n\n".join(body) + "\n}"]

            # Add Route Table Associations
            for idx, subnet_id in enumerate(res.get("associated_subnets", [])):
                sub_ref = f'"{subnet_id}"'
                if subnet_id in resource_ref_map:
                    sub_ref = f"{resource_ref_map[subnet_id]['tf_address']}.id"

                hcl_parts.append(f"""resource "aws_route_table_association" "{clean_name}_assoc_{idx}" {{
  subnet_id      = {sub_ref}
  route_table_id = aws_route_table.{clean_name}.id
}}""")

            return "\n\n".join(hcl_parts), outputs, [], warnings

        elif r_type == "aws_s3_bucket":
            bucket_name = res.get("name") or r_id
            hcl = f"""resource "aws_s3_bucket" "{clean_name}" {{
  bucket = "{bucket_name}"

  tags = {{
    Name = "{bucket_name}"
  }}
}}

resource "aws_s3_bucket_server_side_encryption_configuration" "{clean_name}_encryption" {{
  bucket = aws_s3_bucket.{clean_name}.id

  rule {{
    apply_server_side_encryption_by_default {{
      sse_algorithm = "AES256"
    }}
  }}
}}

resource "aws_s3_bucket_public_access_block" "{clean_name}_public_block" {{
  bucket = aws_s3_bucket.{clean_name}.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}}"""
            outputs.append(f"""output "s3_{clean_name}_bucket" {{
  value       = aws_s3_bucket.{clean_name}.bucket
  description = "S3 Bucket Name for {clean_name}"
}}""")
            return hcl, outputs, [], warnings

        elif r_type == "aws_db_instance":
            engine = res.get("engine")
            inst_class = res.get("instance_class")
            storage = res.get("allocated_storage")
            if not engine:
                unresolved.append(UnresolvedAttribute(
                    resource_id=r_id,
                    resource_type=r_type,
                    attribute_name="engine",
                    reason="RDS instance discovery record is missing 'engine'."
                ))
            if not inst_class:
                unresolved.append(UnresolvedAttribute(
                    resource_id=r_id,
                    resource_type=r_type,
                    attribute_name="instance_class",
                    reason="RDS instance discovery record is missing 'instance_class'."
                ))
            if not storage:
                unresolved.append(UnresolvedAttribute(
                    resource_id=r_id,
                    resource_type=r_type,
                    attribute_name="allocated_storage",
                    reason="RDS instance discovery record is missing 'allocated_storage'."
                ))
            if unresolved:
                return "", [], unresolved, warnings

            engine_ver_attr = f'\n  engine_version       = "{res.get("engine_version")}"' if res.get("engine_version") else ""
            multi_az = "true" if res.get("multi_az") else "false"

            sg_ids = res.get("security_groups") or []
            sg_attr = ""
            if sg_ids:
                refs = [
                    f"{resource_ref_map[sid]['tf_address']}.id" if sid in resource_ref_map else f'"{sid}"'
                    for sid in sg_ids
                ]
                sg_attr = f"\n  vpc_security_group_ids = [{', '.join(refs)}]"

            hcl = f"""resource "aws_db_instance" "{clean_name}" {{
  identifier           = "{res.get('name', clean_name)}"
  engine               = "{engine}"{engine_ver_attr}
  instance_class       = "{inst_class}"
  allocated_storage    = {storage}
  multi_az             = {multi_az}{sg_attr}
  skip_final_snapshot  = true

  tags = {{
    Name = "{res.get('name', clean_name)}"
  }}
}}"""
            outputs.append(f"""output "rds_{clean_name}_endpoint" {{
  value       = aws_db_instance.{clean_name}.endpoint
  description = "RDS Endpoint for {clean_name}"
}}""")
            return hcl, outputs, [], warnings

        elif r_type == "aws_iam_role":
            assume_role_policy = res.get("assume_role_policy")
            if not assume_role_policy or not isinstance(assume_role_policy, dict):
                unresolved.append(UnresolvedAttribute(
                    resource_id=r_id,
                    resource_type=r_type,
                    attribute_name="assume_role_policy",
                    reason="IAM role discovery record is missing valid 'assume_role_policy' document."
                ))
                return "", [], unresolved, warnings

            policy_json = json.dumps(assume_role_policy, indent=2)
            hcl = f"""resource "aws_iam_role" "{clean_name}" {{
  name = "{res.get('name', clean_name)}"

  assume_role_policy = jsonencode({policy_json})

  tags = {{
    Name = "{res.get('name', clean_name)}"
  }}
}}"""
            outputs.append(f"""output "iam_role_{clean_name}_arn" {{
  value       = aws_iam_role.{clean_name}.arn
  description = "IAM Role ARN for {clean_name}"
}}""")
            return hcl, outputs, [], warnings

        else:
            # Unsupported resource type for deterministic template
            unresolved.append(UnresolvedAttribute(
                resource_id=r_id,
                resource_type=r_type,
                attribute_name="resource_type",
                reason=f"Resource type '{r_type}' does not have a verified deterministic template in P2 engine."
            ))
            return "", [], unresolved, warnings

    @staticmethod
    def _default_stack_for_type(resource_type: str) -> str:
        if "db" in resource_type or "rds" in resource_type:
            return "data"
        elif "vpc" in resource_type or "subnet" in resource_type or "gateway" in resource_type or "route" in resource_type:
            return "foundation"
        elif "instance" in resource_type or "ec2" in resource_type:
            return "application"
        elif "s3" in resource_type:
            return "data"
        elif "security_group" in resource_type or "iam" in resource_type:
            return "security"
        return "application"
