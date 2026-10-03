# TerraAgent Drift Reconciliation Report

Diffs each adopted resource's live AWS attributes against what was actually written into the generated Terraform - see `reports/drift_results.json` for the raw data.

- **Resources checked against live AWS:** 6
- **No longer exist in AWS:** 0
- **Destructive-equivalent finding(s):** 0
- **Behavior-changing finding(s):** 0
- **Informational (tag) difference(s):** 0

No drift detected - every checked resource's live attributes match the generated configuration.