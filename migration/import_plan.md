# TerraAgent Import Plan

The same imports as `terraform/imports.tf` (the preferred path - see README), as
`terraform import` CLI commands for Terraform versions without import-block support.
Grouped into dependency-safe migration waves by the Adoption Planning Agent. Run these
from inside the `terraform/` directory, after `terraform init` and after deleting
`imports.tf`, completing each wave before starting the next.
See `migration_checklist.md` for the full adoption procedure. Resources classified
"skip" (AWS-managed) or "use_data_source" (referenced, not owned) are intentionally
excluded - there's no `resource` block for either to import into.

```bash
# Wave 1 - risk: medium
#   - 1 untagged resource(s)
terraform import aws_route_table.rtb_003b648347930edfc_24811887 rtb-003b648347930edfc
terraform import aws_security_group.terraagent_ubuntu_ec2_sg_56255343 sg-077154641a32fe103
terraform import aws_subnet.terraagent_ubuntu_ec2_subnet_4a2dbe19 subnet-06e40849d4b8443f3

# Wave 2 - risk: low
terraform import aws_instance.terraagent_ubuntu_ec2_0ae26311 i-072f8b8170e7a534c
```
