# TerraAgent Infrastructure Migration & Import Guide

This document contains the step-by-step, non-destructive procedure to adopt existing AWS resources into Terraform state.

## Step 1: Initialize Terraform
```bash
terraform init
```

## Step 2: Safe Resource Adoption (terraform import)
Run every command listed in `import_plan.md`, in order, to link your existing live AWS resources to Terraform state without creating duplicates.

## Step 3: Verify Plan
```bash
terraform plan
```
Verify that Terraform reports: `No changes. Your infrastructure matches the configuration.`
