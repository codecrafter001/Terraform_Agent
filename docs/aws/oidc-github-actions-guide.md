# End-to-End AWS STS OIDC, GitHub Actions CI/CD & TerraAgent PR Implementation Guide

This guide details the complete implementation of the end-to-end GitOps workflow:
**TerraAgent $\rightarrow$ GitHub PR $\rightarrow$ DevOps Review $\rightarrow$ GitHub Actions (AWS OIDC/STS) $\rightarrow$ Terraform Apply $\rightarrow$ AWS Infrastructure**.

---

## Architecture Flow

```
+----------------------------------------------------------------------------------------------------+
| 1. User Request                                                                                    |
|    Natural language request: "Upgrade load balancer to HTTPS and issue ACM certificate"           |
+-------------------------------------------------+--------------------------------------------------+
                                                  |
                                                  v
+----------------------------------------------------------------------------------------------------+
| 2. TerraAgent (AI Infrastructure Agent)                                                            |
|    - Performs read-only AWS discovery (via IAM Role + ExternalID)                                  |
|    - Analyzes dependency graph & security impact                                                  |
|    - Generates HCL code (main.tf, acm.tf, load_balancer.tf, variables.tf)                         |
|    - Validates terraform plan locally                                                              |
+-------------------------------------------------+--------------------------------------------------+
                                                  |
                                                  v
+----------------------------------------------------------------------------------------------------+
| 3. GitHub PR Creation                                                                              |
|    - TerraAgent pushes a new branch: terraagent/<feature-name>                                     |
|    - TerraAgent opens a Pull Request with description, change summary & plan diff                 |
+-------------------------------------------------+--------------------------------------------------+
                                                  |
                                                  v
+----------------------------------------------------------------------------------------------------+
| 4. GitHub Actions CI (PR Phase: terraform-plan.yml)                                               |
|    - Authenticates to AWS using GitHub OIDC & STS (No long-lived access keys)                       |
|    - Runs `terraform fmt`, `terraform init`, `terraform validate`, `terraform plan`               |
|    - Automatically posts the formatted plan output as a comment on the Pull Request                |
+-------------------------------------------------+--------------------------------------------------+
                                                  |
                                                  v
+----------------------------------------------------------------------------------------------------+
| 5. DevOps Engineer Review & Approval                                                               |
|    - Reviews HCL diff and the plan comment posted by CI in the PR                                  |
|    - Approves & clicks "Merge Pull Request" to main branch                                         |
+-------------------------------------------------+--------------------------------------------------+
                                                  |
                                                  v
+----------------------------------------------------------------------------------------------------+
| 6. GitHub Actions CD (Deploy Phase: terraform-apply.yml)                                          |
|    - Triggers on push/merge to `main`                                                              |
|    - Exchanges GitHub Actions OIDC token with AWS STS for temporary IAM session                    |
|    - Executes `terraform apply -auto-approve` with state stored in S3 & DynamoDB lock              |
+-------------------------------------------------+--------------------------------------------------+
                                                  |
                                                  v
+----------------------------------------------------------------------------------------------------+
| 7. AWS Infrastructure Updated                                                                      |
|    - ACM Certificate issued, HTTPS Listener configured, Security Groups updated                   |
+----------------------------------------------------------------------------------------------------+
```

---

## Step 1: Provision AWS STS OIDC Provider & IAM Role

We provide an automated bootstrap module located at [`infra/bootstrap-aws-oidc/`](file:///c:/Users/USER/Desktop/AIKART/Terraform%20Agent/infra/bootstrap-aws-oidc/main.tf).

### 1.1 Deploy with Terraform
```bash
cd infra/bootstrap-aws-oidc

# Copy sample vars
cp terraform.tfvars.example terraform.tfvars

# Edit terraform.tfvars with your GitHub repo details:
# github_org  = "<YOUR_GITHUB_USER_OR_ORG>"
# github_repo = "<YOUR_REPO_NAME>"
# aws_region  = "us-east-1"

terraform init
terraform apply
```

### 1.2 Trust Policy Explanation (`sts:AssumeRoleWithWebIdentity`)
The IAM role created uses AWS STS web identity federation to authenticate GitHub Actions runners without static API keys:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": [
            "repo:<YOUR_ORG>/<YOUR_REPO>:ref:refs/heads/*",
            "repo:<YOUR_ORG>/<YOUR_REPO>:pull_request"
          ]
        }
      }
    }
  ]
}
```

---

## Step 2: Configure GitHub Repository Secrets & Permissions

1. In your GitHub repository, navigate to **Settings** $\rightarrow$ **Secrets and variables** $\rightarrow$ **Actions**.
2. Add the following **Repository Secret**:
   - `AWS_DEPLOY_ROLE_ARN`: The ARN outputted by the bootstrap step (e.g. `arn:aws:iam::<ACCOUNT_ID>:role/GitHubActions-TerraformDeployer`).
3. Add the following **Repository Variable** (optional, defaults to `us-east-1`):
   - `AWS_REGION`: `us-east-1`
4. Under **Settings** $\rightarrow$ **Actions** $\rightarrow$ **General** $\rightarrow$ **Workflow permissions**:
   - Select **Read and write permissions**.
   - Enable **Allow GitHub Actions to create and approve pull requests**.

---

## Step 3: Configure Branch Protection for DevOps Approvals

To ensure safe human-in-the-loop deployments:
1. In your GitHub repository, navigate to **Settings** $\rightarrow$ **Branches**.
2. Click **Add branch ruleset** or **Add branch protection rule** for branch pattern `main`.
3. Check **Require a pull request before merging**:
   - Set **Required approvals** to `1`.
   - Check **Dismiss stale pull request approvals when new commits are pushed**.
4. Check **Require status checks to pass before merging**:
   - Search for and add: `Terraform Plan & Validate`.
5. Check **Require linear history** or **Do not allow bypassing the above settings**.

---

## Step 4: GitHub Actions Workflows

The repository includes two pre-configured GitHub Actions workflows:

1. [`.github/workflows/terraform-plan.yml`](file:///c:/Users/USER/Desktop/AIKART/Terraform%20Agent/.github/workflows/terraform-plan.yml)
   - **Trigger**: Pull Request opened/synchronized targeting `main`.
   - **Action**: Authenticates via AWS STS OIDC, checks formatting, validates code, runs `terraform plan`, and writes the execution plan directly as a comment on the GitHub PR.

2. [`.github/workflows/terraform-apply.yml`](file:///c:/Users/USER/Desktop/AIKART/Terraform%20Agent/.github/workflows/terraform-apply.yml)
   - **Trigger**: Commit merged / pushed into `main`.
   - **Action**: Authenticates via AWS STS OIDC, initializes state backend, and executes `terraform apply -auto-approve`.

---

## Step 5: TerraAgent Pull Request Generation

When TerraAgent finishes analyzing a change request and generating the Terraform code:
1. TerraAgent creates a dedicated feature branch: `terraagent/<change-description>`.
2. Commits the generated HCL files (`main.tf`, `variables.tf`, `acm.tf`, etc.) to the branch.
3. Automatically creates a Pull Request into `main` with detailed metadata:
   - **Summary of Changes**: Created, modified, and removed resources.
   - **Risk Assessment**: Security group rules, open ports, and IAM privilege changes.
   - **Pre-computed Plan Diff**.
4. GitHub Actions takes over, runs the live plan via AWS OIDC, and DevOps engineers approve and merge to deploy to AWS.
