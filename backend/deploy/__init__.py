"""Deployment mode: Code -> AWS (docs/design/code-to-aws-deployment.md).

Separate from migration mode (agents/): nothing in agents/, routers/scan.py or
tools/ imports this package. Phases 1-2 only: source intake, analysis, target
recommendation, Terraform rendered from TerraAgent's own templates, build,
validation, security scans and a cost estimate. Nothing here holds AWS
credentials or changes AWS - there is no plan or apply step yet, and when one
arrives, apply gets its own chokepoint (design doc §6.4); tools/terraform_runner
.check_argv is never relaxed.
"""
