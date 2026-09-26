# TerraAgent read-only role

TerraAgent reads your AWS account through an IAM role you create. It never
changes anything: discovery only calls `Describe*`/`Get*`/`List*` APIs (a test
enforces this), `terraform plan` is read-only, and TerraAgent never runs
`apply`, `destroy` or `import`. Your own pipeline applies the pull request
after you review it.

## 1. Create the role

1. **Choose an ExternalId.** Pick a unique, random value for this account (for example
   `uuidgen`). TerraAgent sends it on every `sts:AssumeRole` call. Keep it private
   between you and TerraAgent. Anyone who learns the role ARN still can't use the role without it.
2. **Create the role with the trust policy.** Use [`trust-policy.json`](trust-policy.json),
   replacing `<TERRAAGENT_ACCOUNT_ID>` and `<YOUR_EXTERNAL_ID>`.
3. **Attach the permissions policy.** Attach [`read-only-policy.json`](read-only-policy.json)
   as an inline or customer-managed policy. It has three parts:
   - **`DiscoveryAndPlanReadOnly`:** the reads discovery and `terraform plan` need.
   - **`NeverReadData`:** explicit denies on data-plane reads (S3 objects, secrets,
     parameters, KMS decrypt, table items, queue messages, logs and more). A metadata
     scan never needs them. An explicit deny also wins over any broader allow that
     gets attached later.
   - **`NeverChangeAnything`:** a deny on every action outside the read-only list,
     as a second guard.
4. **Start the scan.** Pass the role in the request as `role_arn` + `external_id`. The
   access keys in the request are used only to call `sts:AssumeRole`. From then on,
   discovery and every later check (drift, plan, config cross-check) use the role's
   1-hour temporary credentials. TerraAgent never stores any credentials.

## 2. What TerraAgent does with it

| Step | Calls |
|---|---|
| Resource Explorer (optional) | `resource-explorer-2:ListIndexes`, `Search` |
| Discovery | `ec2:Describe*` (VPCs + DNS attributes, subnets, route tables, security groups, instances), `s3:ListAllMyBuckets`, `GetBucketLocation`, `GetBucketTagging`, `GetBucketPublicAccessBlock`, `GetEncryptionConfiguration`, `rds:DescribeDBInstances`, `iam:ListRoles`, `ListAttachedRolePolicies` |
| Plan equivalence / drift (opt-in) | the provider's refresh reads for the resources in the plan |

Throttled or failed calls are retried with adaptive backoff. A call that still
fails marks the scan **INCOMPLETE**, never "empty". The job report lists every
failed call.

## 3. If the scan is incomplete

- **`AccessDenied`:** the policy is missing an action for a resource type you use. Add it
  to `DiscoveryAndPlanReadOnly` **and** to the `NotAction` list of `NeverChangeAnything`.
- **`Throttling`:** re-run later, or scan fewer resource types at a time.
