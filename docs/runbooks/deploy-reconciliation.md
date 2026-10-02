# Runbook: Resolving NEEDS_RECONCILIATION Deployments

## Overview
When a deployment is in the `NEEDS_RECONCILIATION` state, it means that an in-flight `terraform apply` was interrupted (for example, by a worker process restart, host failure, network timeout, or container eviction).

Because cloud resources may have been partially provisioned before the interruption, TerraAgent will **NEVER** automatically retry the apply or automate force-unlocking state. A human operator must verify the state of the infrastructure and reconcile it manually.

---

## Step 1: Check Current State and Lock Holder

1. Identify the target S3 state bucket and key from the deployment record:
   - State bucket: `<target.state_bucket>`
   - State key: `terraagent/<target_id>/<deployment_id>.tfstate`
2. Inspect whether a Terraform lock is active on the S3 bucket:
   - For Terraform 1.10+ native S3 lockfile: check for `terraagent/<target_id>/<deployment_id>.tfstate.tflock`
   - For DynamoDB lock table: inspect the lock record corresponding to the state key.
3. Check AWS CloudTrail logs for actions executed by the `TerraAgentDeployApply` role with `SourceIdentity = <approver_email>` around the time of the apply to see which resources were created or modified.

---

## Step 2: Re-Plan with the Read-Only Plan Role

1. From the TerraAgent UI on `/deployments/<deployment_id>`, click **Re-Plan** (or invoke `POST /api/deployments/<deployment_id>/plan`).
2. TerraAgent will run `terraform init` and `terraform plan` using the read-only `TerraAgentDeployPlan` role.
3. Review the newly generated plan:
   - If the previous apply partially created resources, Terraform will detect them in the state (or report drift) and plan only the remaining changes.
   - If an error occurred (e.g. AWS service quota or invalid setting), the plan output will help diagnose the fix needed.

---

## Step 3: Clearing a Stale Lock (If Necessary)

If the previous apply crashed while holding the lock and re-planning fails with `Error: Error acquiring the state lock`, you must manually release the stale lock:

1. Verify that no worker is currently applying against the state.
2. In your administrative terminal with AWS permissions to the state bucket/table:
   ```bash
   terraform force-unlock <LOCK-ID>
   ```
   *Note: TerraAgent never runs `force-unlock` automatically to prevent state corruption.*

---

## Step 4: Re-Approve and Apply

1. Once re-planned and reviewed, approve the new plan bundle in the UI.
2. Click **Deploy** to resume applying the verified, consistent plan.
