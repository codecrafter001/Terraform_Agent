"""Deterministic & Modular HCL code generator for TerraAgent P2/P3 Enterprise Engine.

Transforms structured AWS resource discovery data and adoption plans into clean,
production-grade, modular, dependency-aware, and reproducible Terraform / OpenTofu HCL code.

Architecture:
1. Root Orchestration: variables.tf, outputs.tf, locals.tf, versions.tf, providers.tf,
   terraform.tfvars.example, and backend.tf.example (S3 remote backend + DynamoDB locking).
2. Stack files: every resource lives exactly once, in a root domain stack file
   (foundation.tf, security.tf, data.tf, application.tf) - the same root module
   that is validated, planned and imported (imports.tf uses root addresses).
   No modules/ copies: the root never called them, so they were dead code that
   the scanners still scanned, doubling every finding.
3. Zero Fake Defaults: Missing required attributes trigger review_required with explicit reasons;
   missing optional ones are omitted so the import keeps the live value.
4. Adoption only: exact live tags, no provider default_tags, no security hardening
   (that's the separate Hardening proposal, tools/hardening.py).
5. Real Dependency Preservation: Replaces raw IDs with typed references.
6. Untrusted input: every discovered string is escaped (tools/hcl_render.py).
"""

import json
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from models.manifest import GenerationManifest, UnresolvedAttribute
from tools.hcl_render import escape_template, hcl_bool, hcl_str, route_lines, security_group_rules, tags_block
from tools.import_blocks import IMPORTS_FILENAME, ImportEntry, compose_imports_tf
from tools.infra_model import import_id_for
from tools.naming import unique_clean_name

logger = logging.getLogger("terraagent.hcl_generator")


def route_table_subnets(res: Dict[str, Any]) -> List[str]:
    """Explicitly associated subnets of a discovered route table, sorted so
    association addresses are deterministic."""
    return sorted({s for s in res.get("associated_subnets") or [] if s})


def association_address(route_table_clean_name: str, index: int) -> str:
    return f"aws_route_table_association.{route_table_clean_name}_assoc_{index}"


class HCLGenerator:
    """Deterministic, dependency-aware modular HCL synthesizer."""

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
        """Synthesizes production modular HCL files and a comprehensive generation manifest."""
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


        import_entries: List[ImportEntry] = []
        # Extra import blocks a template emits for companion resources (route
        # table associations), filled by _compose_managed_resource per resource.
        self._companion_imports: List[Tuple[str, str]] = []

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

            import_id = import_id_for(res)
            if not import_id:
                # Can't bind it to the real resource - generating it anyway would
                # make Terraform try to create a duplicate.
                manifest.warnings.append(f"{r_type}.{clean_name}: no import ID could be derived - sent to review")
                manifest.resources_review_required += 1
                manifest.adoption_outcomes[r_id] = "review_required"
                continue
            import_entries.append(ImportEntry(r_id, f"{r_type}.{clean_name}", import_id))
            import_entries.extend(ImportEntry(r_id, address, cid) for address, cid in self._companion_imports)

            manifest.resources_generated += 1
            resource_blocks_by_stack.setdefault(stack_name, []).append(hcl_block)

            if outputs:
                output_blocks_by_stack.setdefault(stack_name, []).extend(outputs)

        # 4. Generate core project files (Root level)
        files: Dict[str, str] = {
            "versions.tf": self._compose_versions_tf(),
            "providers.tf": self._compose_providers_tf(),
            "variables.tf": self._compose_variables_tf(),
            "locals.tf": self._compose_locals_tf(),
            "terraform.tfvars.example": self._compose_tfvars_example(),
            "backend.tf.example": self._compose_backend_example(),
        }

        # Add domain/stack files at root
        for stack_name, blocks in sorted(resource_blocks_by_stack.items()):
            if blocks:
                files[f"{stack_name}.tf"] = "\n\n".join(blocks)

        all_outputs = [b for blocks in output_blocks_by_stack.values() for b in blocks]
        if all_outputs:
            files["outputs.tf"] = "\n\n".join(all_outputs)

        # Import blocks bind each managed resource to the real AWS resource, in
        # dependency-safe order. Root module only - that's where Terraform loads
        # these resource blocks from.
        if import_entries:
            files[IMPORTS_FILENAME] = compose_imports_tf(import_entries, adoption_plan.get("import_order"))

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
        # No default_tags: they would add tags to every imported resource, so
        # the adoption plan would show an update on all of them.
        return """provider "aws" {
  region = var.aws_region
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
  description = "Target deployment stage (e.g. production, staging, development)"
  default     = "production"
}}

variable "project_name" {{
  type        = string
  description = "Project identifier tag"
  default     = "TerraAgent-Adopted"
}}
"""

    def _compose_locals_tf(self) -> str:
        # No shared tag map here: adoption code carries each resource's own live
        # tags only (see _compose_providers_tf).
        return """locals {
  environment = var.environment
  project     = var.project_name
}
"""

    def _compose_tfvars_example(self) -> str:
        return f"""# Example Terraform Input Variables
aws_region   = "{self.region}"
environment  = "production"
project_name = "Cloud-Modernization"
"""

    def _compose_backend_example(self) -> str:
        return """# S3 Remote State Backend with DynamoDB State Locking Example
# To enable remote state, uncomment this block and configure your S3 bucket.
#
# terraform {
#   backend "s3" {
#     bucket         = "your-terraform-state-bucket"
#     key            = "infrastructure/production/terraform.tfstate"
#     region         = "us-east-1"
#     encrypt        = true
#     dynamodb_table = "terraform-lock-table"
#   }
# }
"""

    def _compose_data_source(self, r_type: str, res: Dict[str, Any], clean_name: str) -> str:
        return f'data "{r_type}" "{clean_name}" {{\n  id = {hcl_str(res.get("id", ""))}\n}}'

    def _compose_managed_resource(
        self,
        r_type: str,
        res: Dict[str, Any],
        clean_name: str,
        resource_ref_map: Dict[str, Dict[str, Any]]
    ) -> Tuple[str, List[str], List[UnresolvedAttribute], List[str]]:
        """Synthesizes adoption HCL: exactly what is live, nothing more.
        Returns (hcl, outputs, unresolved, warnings).

        Adoption rules (the adoption PR must plan with zero changes):
        - Every value comes from discovery. A required value that wasn't
          discovered makes the resource unresolved (-> review), never a default.
        - An optional attribute that wasn't discovered is omitted, so Terraform
          keeps whatever the import reads from AWS.
        - Tags are copied exactly (tags_block); no provider default_tags.
        - No security hardening (encryption, IMDSv2, public access blocks...):
          that is the Hardening proposal's job (tools/hardening.py).
        - Every discovered string goes through hcl_str - names and tags are
          untrusted text."""
        r_id = res.get("id", "")
        outputs: List[str] = []
        unresolved: List[UnresolvedAttribute] = []
        warnings: List[str] = []
        address = f"{r_type}.{clean_name}"
        self._companion_imports = []

        def missing(attribute: str, reason: str) -> None:
            unresolved.append(UnresolvedAttribute(
                resource_id=r_id, resource_type=r_type, attribute_name=attribute, reason=reason))

        def ref(target_id: Optional[str], attr: str = "id") -> str:
            if target_id in resource_ref_map:
                return f"{resource_ref_map[target_id]['tf_address']}.{attr}"
            return hcl_str(target_id)

        def output(name: str, attr: str, description: str) -> None:
            outputs.append(f'output "{name}" {{\n  value       = {address}.{attr}\n'
                           f'  description = {hcl_str(description)}\n}}')

        def resource(body: List[str]) -> str:
            lines = [line for line in body if line]
            tags = tags_block(res.get("tags"))
            if tags:
                lines.append(tags)
            return f'resource "{r_type}" "{clean_name}" {{\n' + "\n".join(lines) + "\n}"

        if r_type == "aws_vpc":
            if not res.get("cidr_block"):
                missing("cidr_block", "VPC discovery record is missing 'cidr_block'.")
                return "", [], unresolved, warnings
            body = [f"  cidr_block = {hcl_str(res['cidr_block'])}"]
            for attr in ("enable_dns_support", "enable_dns_hostnames"):
                if isinstance(res.get(attr), bool):
                    body.append(f"  {attr} = {hcl_bool(res[attr])}")
            hcl = resource(body)
            output(f"vpc_{clean_name}_id", "id", f"VPC ID for {clean_name}")
            return hcl, outputs, [], warnings

        elif r_type == "aws_subnet":
            if not res.get("cidr_block"):
                missing("cidr_block", "Subnet discovery record is missing 'cidr_block'.")
            if not res.get("vpc_id"):
                missing("vpc_id", "Subnet discovery record is missing 'vpc_id'.")
            if unresolved:
                return "", [], unresolved, warnings
            body = [f"  vpc_id     = {ref(res['vpc_id'])}", f"  cidr_block = {hcl_str(res['cidr_block'])}"]
            if res.get("availability_zone"):
                body.append(f"  availability_zone = {hcl_str(res['availability_zone'])}")
            if isinstance(res.get("map_public_ip_on_launch"), bool):
                body.append(f"  map_public_ip_on_launch = {hcl_bool(res['map_public_ip_on_launch'])}")
            else:
                warnings.append(f"{address}: map_public_ip_on_launch not discovered - Terraform's default (false) applies")
            hcl = resource(body)
            output(f"subnet_{clean_name}_id", "id", f"Subnet ID for {clean_name}")
            return hcl, outputs, [], warnings

        elif r_type == "aws_internet_gateway":
            body = [f"  vpc_id = {ref(res['vpc_id'])}"] if res.get("vpc_id") else []
            hcl = resource(body)
            output(f"igw_{clean_name}_id", "id", f"Internet Gateway ID for {clean_name}")
            return hcl, outputs, [], warnings

        elif r_type == "aws_nat_gateway":
            if not res.get("subnet_id"):
                missing("subnet_id", "NAT Gateway discovery record is missing 'subnet_id'.")
            if not res.get("allocation_id") and (res.get("connectivity_type") or "public") == "public":
                missing("allocation_id", "Public NAT Gateway discovery record is missing its Elastic IP 'allocation_id'.")
            if unresolved:
                return "", [], unresolved, warnings
            body = [f"  subnet_id = {ref(res['subnet_id'])}"]
            if res.get("allocation_id"):
                body.append(f"  allocation_id = {ref(res['allocation_id'])}")
            if res.get("connectivity_type"):
                body.append(f"  connectivity_type = {hcl_str(res['connectivity_type'])}")
            hcl = resource(body)
            output(f"nat_gw_{clean_name}_id", "id", f"NAT Gateway ID for {clean_name}")
            return hcl, outputs, [], warnings

        elif r_type == "aws_security_group":
            # description is ForceNew: a guessed value would replace the group.
            if res.get("description") is None:
                missing("description", "Security group description not discovered (changing it forces replacement).")
                return "", [], unresolved, warnings
            body = [f"  name        = {hcl_str(res.get('name') or r_id)}",
                    f"  description = {hcl_str(res['description'])}"]
            if res.get("vpc_id"):
                body.append(f"  vpc_id      = {ref(res['vpc_id'])}")
            # Every live rule, including SG-to-SG, IPv6 and prefix-list sources -
            # a rule left out here would be deleted by the plan.
            for block, perms in (("ingress", res.get("ip_permissions")), ("egress", res.get("ip_permissions_egress"))):
                for rule in security_group_rules(perms or [], r_id, ref):
                    body.append(f"\n  {block} {{\n" + "\n".join(f"    {line}" for line in rule) + "\n  }")
            hcl = resource(body)
            output(f"sg_{clean_name}_id", "id", f"Security Group ID for {clean_name}")
            return hcl, outputs, [], warnings

        elif r_type == "aws_instance":
            if not res.get("ami"):
                missing("ami", "EC2 Instance discovery record is missing required 'ami'.")
            if not res.get("instance_type"):
                missing("instance_type", "EC2 Instance discovery record is missing required 'instance_type'.")
            if unresolved:
                return "", [], unresolved, warnings
            body = [f"  ami           = {hcl_str(res['ami'])}",
                    f"  instance_type = {hcl_str(res['instance_type'])}"]
            if res.get("subnet_id"):
                body.append(f"  subnet_id     = {ref(res['subnet_id'])}")
            sg_ids = res.get("security_groups") or []
            if sg_ids:
                body.append(f"  vpc_security_group_ids = [{', '.join(ref(s) for s in sg_ids)}]")
            profile_arn = res.get("iam_instance_profile_arn")
            if profile_arn:
                # The instance PROFILE name, not a role reference.
                body.append(f"  iam_instance_profile = {hcl_str(profile_arn.rsplit('/', 1)[-1])}")
            hcl = resource(body)
            output(f"instance_{clean_name}_id", "id", f"EC2 Instance ID for {clean_name}")
            return hcl, outputs, [], warnings

        elif r_type in ("aws_lb", "aws_alb"):
            # name, internal and load_balancer_type all force a new load balancer.
            for attr in ("name", "scheme", "load_balancer_type"):
                if not res.get(attr):
                    missing(attr, f"Load balancer discovery record is missing '{attr}' (changing it forces replacement).")
            if unresolved:
                return "", [], unresolved, warnings
            body = [f"  name               = {hcl_str(res['name'])}",
                    f"  internal           = {hcl_bool(res['scheme'] == 'internal')}",
                    f"  load_balancer_type = {hcl_str(res['load_balancer_type'])}"]
            if res.get("subnets"):
                body.append(f"  subnets = [{', '.join(ref(s) for s in res['subnets'])}]")
            if res.get("security_groups"):
                body.append(f"  security_groups = [{', '.join(ref(s) for s in res['security_groups'])}]")
            # Arguments whose provider default may differ from the live value
            # (read with DescribeLoadBalancerAttributes; left out when unread).
            for attr in ("enable_deletion_protection", "enable_http2", "drop_invalid_header_fields",
                         "enable_cross_zone_load_balancing"):
                if isinstance(res.get(attr), bool):
                    body.append(f"  {attr} = {hcl_bool(res[attr])}")
            if isinstance(res.get("idle_timeout"), int):
                body.append(f"  idle_timeout = {int(res['idle_timeout'])}")
            hcl = resource(body).replace(f'resource "{r_type}"', 'resource "aws_lb"', 1)
            outputs.append(f'output "alb_{clean_name}_dns_name" {{\n  value       = aws_lb.{clean_name}.dns_name\n'
                           f'  description = {hcl_str("DNS name of Application Load Balancer for " + clean_name)}\n}}')
            return hcl, outputs, [], warnings

        elif r_type == "aws_route_table":
            if not res.get("vpc_id"):
                missing("vpc_id", "Route Table discovery record is missing 'vpc_id'.")
                return "", [], unresolved, warnings
            body = [f"  vpc_id = {ref(res['vpc_id'])}"]
            for route in res.get("routes", []) or []:
                lines = route_lines(route)
                if lines is None:
                    continue  # the implicit local route, or one AWS propagated
                if not lines:
                    missing("route", f"A route on {r_id} has a destination or target TerraAgent can't represent yet.")
                    return "", [], unresolved, warnings
                body.append("\n  route {\n" + "\n".join(f"    {line}" for line in lines) + "\n  }")
            hcl_parts = [resource(body)]
            # Each explicit subnet association is its own resource, imported
            # as "subnet-id/rtb-id" (the main-table association has no subnet
            # and is never managed here).
            for idx, subnet_id in enumerate(route_table_subnets(res)):
                assoc = association_address(clean_name, idx)
                hcl_parts.append(
                    f'resource "aws_route_table_association" "{assoc.split(".", 1)[1]}" {{\n'
                    f"  subnet_id      = {ref(subnet_id)}\n"
                    f"  route_table_id = {address}.id\n}}"
                )
                self._companion_imports.append((assoc, f"{subnet_id}/{r_id}"))
            return "\n\n".join(hcl_parts), outputs, [], warnings

        elif r_type == "aws_s3_bucket":
            hcl = resource([f"  bucket = {hcl_str(res.get('name') or r_id)}"])
            output(f"s3_{clean_name}_bucket", "bucket", f"S3 Bucket Name for {clean_name}")
            return hcl, outputs, [], warnings

        elif r_type == "aws_db_instance":
            for attr, label in (("engine", "engine"), ("instance_class", "instance_class"),
                                ("allocated_storage", "allocated_storage")):
                if not res.get(attr):
                    missing(label, f"RDS instance discovery record is missing '{attr}'.")
            if unresolved:
                return "", [], unresolved, warnings
            body = [f"  identifier        = {hcl_str(res.get('name') or r_id)}",
                    f"  engine            = {hcl_str(res['engine'])}"]
            if res.get("engine_version"):
                body.append(f"  engine_version    = {hcl_str(res['engine_version'])}")
            body += [f"  instance_class    = {hcl_str(res['instance_class'])}",
                     f"  allocated_storage = {int(res['allocated_storage'])}",
                     f"  multi_az          = {hcl_bool(bool(res.get('multi_az')))}"]
            sg_ids = res.get("security_groups") or []
            if sg_ids:
                body.append(f"  vpc_security_group_ids = [{', '.join(ref(s) for s in sg_ids)}]")
            hcl = resource(body)
            output(f"rds_{clean_name}_endpoint", "endpoint", f"RDS Endpoint for {clean_name}")
            return hcl, outputs, [], warnings

        elif r_type == "aws_dynamodb_table":
            if res.get("has_indexes"):
                missing("indexes", "DynamoDB table has secondary indexes (GSIs/LSIs) which require manual review.")
                return "", [], unresolved, warnings
            if not res.get("hash_key") or not res.get("hash_key_type"):
                missing("hash_key", "DynamoDB table discovery record is missing its hash key name/type.")
                return "", [], unresolved, warnings
            billing_mode = str(res.get("billing_mode") or "PROVISIONED")
            body = [f"  name         = {hcl_str(res.get('name') or r_id)}",
                    f"  billing_mode = {hcl_str(billing_mode)}",
                    f"  hash_key     = {hcl_str(res['hash_key'])}"]
            if res.get("range_key"):
                if not res.get("range_key_type"):
                    missing("range_key", "DynamoDB table discovery record is missing its range key type.")
                    return "", [], unresolved, warnings
                body.append(f"  range_key    = {hcl_str(res['range_key'])}")
            if billing_mode == "PROVISIONED":
                if res.get("read_capacity") is not None:
                    body.append(f"  read_capacity  = {int(res['read_capacity'])}")
                if res.get("write_capacity") is not None:
                    body.append(f"  write_capacity = {int(res['write_capacity'])}")
            body.append(f"\n  attribute {{\n    name = {hcl_str(res['hash_key'])}\n    type = {hcl_str(res['hash_key_type'])}\n  }}")
            if res.get("range_key") and res.get("range_key_type"):
                body.append(f"\n  attribute {{\n    name = {hcl_str(res['range_key'])}\n    type = {hcl_str(res['range_key_type'])}\n  }}")
            if res.get("stream_enabled") is True:
                if not res.get("stream_view_type"):
                    missing("stream_view_type", "DynamoDB stream is enabled but its view type wasn't discovered.")
                    return "", [], unresolved, warnings
                body.append("  stream_enabled   = true")
                body.append(f"  stream_view_type = {hcl_str(res['stream_view_type'])}")
            if isinstance(res.get("deletion_protection_enabled"), bool):
                body.append(f"  deletion_protection_enabled = {hcl_bool(res['deletion_protection_enabled'])}")
            if res.get("table_class"):
                body.append(f"  table_class = {hcl_str(res['table_class'])}")
            hcl = resource(body)
            output(f"dynamodb_{clean_name}_arn", "arn", f"DynamoDB Table ARN for {clean_name}")
            return hcl, outputs, [], warnings

        elif r_type == "aws_iam_role":
            assume_role_policy = res.get("assume_role_policy")
            if not assume_role_policy or not isinstance(assume_role_policy, dict):
                missing("assume_role_policy", "IAM role discovery record is missing valid 'assume_role_policy' document.")
                return "", [], unresolved, warnings
            # json.dumps output is valid HCL for jsonencode(); escape template
            # sequences, since the policy document is discovered text too.
            policy = escape_template(json.dumps(assume_role_policy, indent=2, sort_keys=True))
            body = [f"  name = {hcl_str(res.get('name') or r_id)}"]
            if res.get("path"):
                body.append(f"  path = {hcl_str(res['path'])}")
            if res.get("description"):
                body.append(f"  description = {hcl_str(res['description'])}")
            if res.get("max_session_duration"):
                body.append(f"  max_session_duration = {int(res['max_session_duration'])}")
            body.append(f"\n  assume_role_policy = jsonencode({policy})")
            hcl = resource(body)
            output(f"iam_role_{clean_name}_arn", "arn", f"IAM Role ARN for {clean_name}")
            return hcl, outputs, [], warnings

        elif r_type == "aws_kms_key":
            if res.get("origin") not in (None, "AWS_KMS"):
                missing("origin", f"KMS key origin {res.get('origin')} (imported key material or a custom key store) "
                                  "isn't adopted as aws_kms_key.")
                return "", [], unresolved, warnings
            body = [f"  description = {hcl_str(res['description'])}"] if res.get("description") else []
            if res.get("key_usage"):
                body.append(f"  key_usage = {hcl_str(res['key_usage'])}")
            if res.get("customer_master_key_spec"):
                body.append(f"  customer_master_key_spec = {hcl_str(res['customer_master_key_spec'])}")
            for attr in ("multi_region", "is_enabled", "enable_key_rotation"):
                if isinstance(res.get(attr), bool):
                    body.append(f"  {attr} = {hcl_bool(res[attr])}")
            hcl = resource(body)
            output(f"kms_{clean_name}_arn", "arn", f"KMS Key ARN for {clean_name}")
            return hcl, outputs, [], warnings

        elif r_type == "aws_sqs_queue":
            body = [f"  name = {hcl_str(res.get('name') or r_id)}"]
            if res.get("visibility_timeout_seconds") is not None:
                body.append(f"  visibility_timeout_seconds = {int(res['visibility_timeout_seconds'])}")
            if res.get("message_retention_seconds") is not None:
                body.append(f"  message_retention_seconds = {int(res['message_retention_seconds'])}")
            if res.get("delay_seconds") is not None:
                body.append(f"  delay_seconds = {int(res['delay_seconds'])}")
            if res.get("max_message_size") is not None:
                body.append(f"  max_message_size = {int(res['max_message_size'])}")
            if res.get("receive_wait_time_seconds") is not None:
                body.append(f"  receive_wait_time_seconds = {int(res['receive_wait_time_seconds'])}")
            if isinstance(res.get("fifo_queue"), bool):
                body.append(f"  fifo_queue = {hcl_bool(res['fifo_queue'])}")
            if res.get("fifo_queue") is True and isinstance(res.get("content_based_deduplication"), bool):
                body.append(f"  content_based_deduplication = {hcl_bool(res['content_based_deduplication'])}")
            if res.get("kms_master_key_id"):
                body.append(f"  kms_master_key_id = {hcl_str(res['kms_master_key_id'])}")
                if res.get("kms_data_key_reuse_period_seconds") is not None:
                    body.append(f"  kms_data_key_reuse_period_seconds = {int(res['kms_data_key_reuse_period_seconds'])}")
            elif isinstance(res.get("sqs_managed_sse_enabled"), bool):
                body.append(f"  sqs_managed_sse_enabled = {hcl_bool(res['sqs_managed_sse_enabled'])}")
            if res.get("redrive_policy"):
                try:
                    rd_obj = json.loads(res["redrive_policy"]) if isinstance(res["redrive_policy"], str) else res["redrive_policy"]
                    policy = escape_template(json.dumps(rd_obj, indent=2, sort_keys=True))
                    body.append(f"  redrive_policy = jsonencode({policy})")
                except Exception:
                    body.append(f"  redrive_policy = {hcl_str(res['redrive_policy'])}")
            hcl = resource(body)
            output(f"sqs_{clean_name}_url", "url", f"SQS Queue URL for {clean_name}")
            return hcl, outputs, [], warnings

        elif r_type == "aws_sns_topic":
            body = [f"  name = {hcl_str(res.get('name') or r_id)}"]
            if res.get("display_name"):
                body.append(f"  display_name = {hcl_str(res['display_name'])}")
            if isinstance(res.get("fifo_topic"), bool):
                body.append(f"  fifo_topic = {hcl_bool(res['fifo_topic'])}")
            if res.get("fifo_topic") is True and isinstance(res.get("content_based_deduplication"), bool):
                body.append(f"  content_based_deduplication = {hcl_bool(res['content_based_deduplication'])}")
            if res.get("kms_master_key_id"):
                body.append(f"  kms_master_key_id = {hcl_str(res['kms_master_key_id'])}")
            hcl = resource(body)
            output(f"sns_{clean_name}_arn", "arn", f"SNS Topic ARN for {clean_name}")
            return hcl, outputs, [], warnings

        else:
            # Unsupported resource type for deterministic template
            missing("resource_type", f"Resource type '{r_type}' does not have a verified deterministic template in P2 engine.")
            return "", [], unresolved, warnings

    @staticmethod
    def _default_stack_for_type(resource_type: str) -> str:
        if "db" in resource_type or "rds" in resource_type or "dynamo" in resource_type:
            return "data"
        elif "vpc" in resource_type or "subnet" in resource_type or "gateway" in resource_type or "route" in resource_type:
            return "foundation"
        elif "instance" in resource_type or "ec2" in resource_type or "lb" in resource_type or "alb" in resource_type:
            return "application"
        elif "s3" in resource_type or "sqs" in resource_type or "sns" in resource_type:
            return "data"
        elif "security_group" in resource_type or "iam" in resource_type or "kms" in resource_type:
            return "security"
        return "application"
