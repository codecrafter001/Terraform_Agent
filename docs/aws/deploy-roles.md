# Customer AWS Account Setup for TerraAgent Deployments

TerraAgent provides automated infrastructure analysis, verification, planning, and deployment for your applications. To connect your AWS account safely, TerraAgent uses a **least-privilege, dual-role architecture** with strict security boundaries and short-lived STS sessions.

---

## 1. Architecture & Security Model

When you connect your AWS account, you run a bootstrap template (CloudFormation or Terraform) that provisions four items:

| Resource | Purpose | Permissions & Scope |
|---|---|---|
| **`TerraAgentDeployPlan` Role** | Read-only planning | `Describe*`, `Get*`, `List*` on supported services + read access to the state bucket. Runs `terraform plan` without writing to your infrastructure. |
| **`TerraAgentDeployApply` Role** | Approved apply execution | Strictly scoped to resources tagged `terraagent:managed = true` and named `terraagent-*`. Denied access to admin policies, user creation, or modifications to itself. Used only in Phase 4. |
| **`TerraAgentWorkloadBoundary`** | Workload Permissions Boundary | Managed policy attached as a hard ceiling to any IAM role created for your application (e.g. Lambda execution roles). Blocks privilege escalation. |
| **`TerraformStateBucket`** | Remote state storage | S3 bucket with default encryption (AES256), versioning enabled, and all public access blocked. |

```
                       ┌─────────────────────────────────────────────────────┐
                       │               Your AWS Account                      │
                       │                                                     │
┌─────────────────┐    │  ┌───────────────────────┐   ┌───────────────────┐  │
│ TerraAgent API  │────┼─▶│ TerraAgentDeployPlan  │──▶│ Terraform State   │  │
│ (Phase 3 Plan)  │    │  │ (Read-Only STS)       │   │ Bucket (S3)       │  │
└─────────────────┘    │  └───────────────────────┘   └───────────────────┘  │
                       │                                                     │
┌─────────────────┐    │  ┌───────────────────────┐   ┌───────────────────┐  │
│ TerraAgent      │────┼─▶│ TerraAgentDeployApply │──▶│ terraagent-*      │  │
│ Deployer Worker │    │  │ (Scoped Mutation STS) │   │ App Resources     │  │
│ (Phase 4 Apply) │    │  └──────────┬────────────┘   └───────────────────┘  │
└─────────────────┘    │             │                                       │
                       │             ▼                                       │
                       │  ┌───────────────────────┐                          │
                       │  │ Workload Boundary     │                          │
                       │  │ (Ceiling on App Roles)│                          │
                       │  └───────────────────────┘                          │
                       └─────────────────────────────────────────────────────┘
```

---

## 2. Security Guarantees

1. **Confused Deputy Protection (`sts:ExternalId`)**:
   - Every deploy target generates a cryptographically unique `ExternalId` (`secrets.token_urlsafe(24)`).
   - The role trust policy enforces `StringEquals: { sts:ExternalId: <tenant-external-id> }`.
   - Another customer who learns your Role ARN cannot assume your role.

2. **Short-Lived Ephemeral Sessions**:
   - Plan sessions: 15 minutes (900 seconds), scoped down via inline session policy.
   - Apply sessions: 1 hour (3600 seconds) with `SourceIdentity` bound to the approver's email in AWS CloudTrail.
   - No static access keys or long-term credentials are ever stored.

3. **Explicit Denies**:
   - No `iam:CreateUser`, `iam:CreateAccessKey`, or `iam:CreateLoginProfile`.
   - No `organizations:*` or `account:*` operations.
   - No attaching `AdministratorAccess` or `IAMFullAccess`.
   - No modifying the bootstrap roles or permissions boundary.

---

## 3. Provisioning Options

### Option A: CloudFormation (Recommended)

1. In the AWS Console, open **CloudFormation** → **Create Stack** (with new resources).
2. Upload `backend/deploy/bootstrap/cloudformation.yaml`.
3. Provide the parameters:
   - `TerraAgentPrincipalArn`: The ARN of TerraAgent's platform role/user (provided in your TerraAgent dashboard).
   - `ExternalId`: Generated in TerraAgent **Settings → Deploy Targets**.
   - `StateBucketName`: A unique S3 bucket name for state storage (e.g. `acme-terraagent-state-us-east-1`).
4. Acknowledge IAM capabilities (`CAPABILITY_NAMED_IAM`) and create the stack.

### Option B: Terraform

1. Navigate to `backend/deploy/bootstrap/terraform/`.
2. Create `terraform.tfvars`:
   ```hcl
   region                   = "us-east-1"
   state_bucket_name        = "acme-terraagent-state-us-east-1"
   terraagent_principal_arn = "arn:aws:iam::123456789012:role/TerraAgentPlatform"
   external_id              = "<YOUR_GENERATED_EXTERNAL_ID>"
   ```
3. Run:
   ```bash
   terraform init
   terraform apply
   ```

---

## 4. Registering in TerraAgent

1. Go to **Settings → Deploy Targets** in TerraAgent.
2. Click **Add Deploy Target** and enter:
   - Target Name (e.g. `Production AWS - us-east-1`)
   - AWS Account ID (12 digits)
   - AWS Region (e.g. `us-east-1`)
   - Plan Role ARN (`arn:aws:iam::<ACCOUNT_ID>:role/TerraAgentDeployPlan`)
   - Apply Role ARN (`arn:aws:iam::<ACCOUNT_ID>:role/TerraAgentDeployApply`)
   - Permissions Boundary ARN (`arn:aws:iam::<ACCOUNT_ID>:policy/TerraAgentWorkloadBoundary`)
   - State Bucket Name (`<YOUR_STATE_BUCKET_NAME>`)
3. Click **Verify Target**:
   - TerraAgent tests AssumeRole for `TerraAgentDeployPlan` with your `ExternalId`.
   - Checks `sts:GetCallerIdentity` matches your account ID.
   - Verifies the state bucket is reachable.
   - Tests AssumeRole for `TerraAgentDeployApply`.
4. Once verified, the target is ready for zero-trust, read-only planning and gated approvals.

---

## 5. ECS Fargate & In-Account CodeBuild Architecture (Phase 5.3)

For containerized applications (`ecs_service` target):
- **Untrusted Code Isolation**: TerraAgent never executes `docker build` on its own servers. The source bundle is packaged and built inside the customer's AWS account via AWS CodeBuild (`codebuild:StartBuild`), running under the approved apply role.
- **Role Permissions**:
  - `TerraAgentDeployPlan`: Has read-only describe access on ECS, ECR, CodeBuild, ALB, and EC2 networking.
  - `TerraAgentDeployApply`: Scoped to manage `terraagent-*` tagged ECS clusters, services, task definitions, ECR repositories, CodeBuild projects, and ALBs.
  - `TerraAgentWorkloadBoundary`: Enforces least privilege on the ECS execution and task roles.

