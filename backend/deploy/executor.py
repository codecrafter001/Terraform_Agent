import logging
from typing import Any, Dict, Optional, Protocol

import httpx

from deploy.apply_runner import apply_approved
from deploy.store import DeployStatus, DeploymentNotFound, get_deployment, transition
from services.github_client import GITHUB_API_BASE, _REQUEST_TIMEOUT, GitHubPullRequestError, _publish
from services.redis_client import redis_service

logger = logging.getLogger("terraagent.deploy.executor")


class Executor(Protocol):
    async def execute(self, deployment_id: str) -> Dict[str, Any]:
        """Execute the deployment action."""
        ...


class StsApplyExecutor:
    """Phase 4A: STS Direct Apply Executor."""

    async def execute(self, deployment_id: str) -> Dict[str, Any]:
        return await apply_approved(deployment_id)


class GitOpsPrExecutor:
    """Phase 4B: GitOps PR Executor.

    Commits the generated Terraform configuration (and optional GitHub Actions CI/CD workflows)
    to a new feature branch and opens a Pull Request for team review and GitOps deployment.
    """

    def __init__(
        self,
        github_token: str,
        repo: str,
        base_branch: str = "main",
        target_dir: str = "terraform",
        add_workflows: bool = False,
    ):
        self.github_token = github_token
        self.repo = repo
        self.base_branch = base_branch
        self.target_dir = target_dir.strip("/") if target_dir else "terraform"
        self.add_workflows = add_workflows

    def _generate_plan_workflow(self) -> str:
        tf_dir = self.target_dir or "terraform"
        return f"""name: "Terraform Plan (PR Review)"

on:
  pull_request:
    branches:
      - {self.base_branch}
    paths:
      - "{tf_dir}/**"
      - "*.tf"

permissions:
  id-token: write
  contents: read
  pull-requests: write

env:
  AWS_REGION: ${{{{ vars.AWS_REGION || 'us-east-1' }}}}
  ROLE_TO_ASSUME: ${{{{ secrets.AWS_DEPLOY_ROLE_ARN }}}}
  TF_WORKING_DIR: "{tf_dir}"
  TERRAFORM_VERSION: "1.9.5"

jobs:
  plan:
    name: "Terraform Plan & Validate"
    runs-on: ubuntu-latest

    steps:
      - name: Checkout Repository
        uses: actions/checkout@v4

      - name: Setup Terraform
        uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: ${{{{ env.TERRAFORM_VERSION }}}}

      - name: Configure AWS Credentials via OIDC
        if: env.ROLE_TO_ASSUME != ''
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{{{ env.ROLE_TO_ASSUME }}}}
          aws-region: ${{{{ env.AWS_REGION }}}}
          audience: "sts.amazonaws.com"
          role-session-name: "github-actions-tf-plan-${{{{ github.event.pull_request.number }}}}"

      - name: Terraform Format Check
        id: fmt
        working-directory: ${{{{ env.TF_WORKING_DIR }}}}
        run: terraform fmt -check -recursive
        continue-on-error: true

      - name: Terraform Init
        id: init
        working-directory: ${{{{ env.TF_WORKING_DIR }}}}
        run: terraform init -input=false

      - name: Terraform Validate
        id: validate
        working-directory: ${{{{ env.TF_WORKING_DIR }}}}
        run: terraform validate -no-color

      - name: Terraform Plan
        id: plan
        working-directory: ${{{{ env.TF_WORKING_DIR }}}}
        run: |
          terraform plan -input=false -no-color -out=tfplan | tee plan_output.txt
        continue-on-error: false
"""

    def _generate_apply_workflow(self) -> str:
        tf_dir = self.target_dir or "terraform"
        return f"""name: "Terraform Apply (GitOps Deploy)"

on:
  push:
    branches:
      - {self.base_branch}
    paths:
      - "{tf_dir}/**"
      - "*.tf"

permissions:
  id-token: write
  contents: read

env:
  AWS_REGION: ${{{{ vars.AWS_REGION || 'us-east-1' }}}}
  ROLE_TO_ASSUME: ${{{{ secrets.AWS_DEPLOY_ROLE_ARN }}}}
  TF_WORKING_DIR: "{tf_dir}"
  TERRAFORM_VERSION: "1.9.5"

jobs:
  apply:
    name: "Terraform Apply"
    runs-on: ubuntu-latest

    steps:
      - name: Checkout Repository
        uses: actions/checkout@v4

      - name: Setup Terraform
        uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: ${{{{ env.TERRAFORM_VERSION }}}}

      - name: Configure AWS Credentials via OIDC
        if: env.ROLE_TO_ASSUME != ''
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{{{ env.ROLE_TO_ASSUME }}}}
          aws-region: ${{{{ env.AWS_REGION }}}}
          audience: "sts.amazonaws.com"
          role-session-name: "github-actions-tf-apply-${{{{ github.sha }}}}"

      - name: Terraform Init
        working-directory: ${{{{ env.TF_WORKING_DIR }}}}
        run: terraform init -input=false

      - name: Terraform Apply
        working-directory: ${{{{ env.TF_WORKING_DIR }}}}
        run: |
          terraform plan -input=false -no-color -out=tfplan
          terraform apply -input=false -no-color tfplan
"""

    def _generate_pr_body(self, dep: Dict[str, Any]) -> str:
        source_name = dep.get("source_name") or dep.get("id")
        source_kind = dep.get("source_kind") or "source"
        target_type = dep.get("target_type") or "AWS Target"
        region = dep.get("region") or "us-east-1"
        env = dep.get("environment") or "production"
        approver = dep.get("approved_by") or "Unknown"
        approved_at = dep.get("approved_at") or "Unknown"
        bundle_hash = dep.get("plan_bundle_sha256") or "N/A"

        plan_summary = dep.get("plan_summary") or {}
        add_count = plan_summary.get("to_add", plan_summary.get("add", 0))
        change_count = plan_summary.get("to_change", plan_summary.get("change", 0))
        destroy_count = plan_summary.get("to_destroy", plan_summary.get("destroy", 0))

        policy = dep.get("plan_policy") or {}
        policy_verdict = policy.get("verdict", "PASSED")

        verification = dep.get("verification") or {}
        cost_monthly = verification.get("infracost", {}).get("total_monthly_cost")
        cost_str = f"${cost_monthly}/mo" if cost_monthly is not None else "Free tier / Usage-based"

        lines = [
            f"## 🚀 TerraAgent Infrastructure Deployment",
            f"",
            f"This Pull Request contains Terraform infrastructure configuration automatically generated and verified by **TerraAgent** for **`{source_name}`**.",
            f"",
            f"### 📋 Deployment Summary",
            f"| Property | Details |",
            f"| :--- | :--- |",
            f"| **Source** | `{source_name}` ({source_kind}) |",
            f"| **Architecture Target** | `{target_type}` |",
            f"| **AWS Region** | `{region}` |",
            f"| **Environment** | `{env}` |",
            f"| **Target Directory** | `{self.target_dir}/` |",
            f"| **Plan Changes** | `+{add_count} ~{change_count} -{destroy_count}` |",
            f"| **Estimated Cost** | {cost_str} |",
            f"| **Policy Check** | `{policy_verdict}` |",
            f"",
            f"### 🔐 Verification & Approval Gate",
            f"- **Approved By**: `{approver}`",
            f"- **Approved At**: `{approved_at}`",
            f"- **Plan Bundle SHA-256**: `{bundle_hash}`",
            f"",
            f"### ⚙️ Next Steps",
            f"1. Review the Terraform definitions in `{self.target_dir}/`.",
            f"2. Merge this Pull Request into `{self.base_branch}`.",
            f"3. GitHub Actions CI/CD will execute `terraform apply` using AWS OIDC authentication.",
            f"",
            f"---",
            f"*Generated by TerraAgent Deployment Engine.*",
        ]
        return "\n".join(lines)

    async def execute(self, deployment_id: str) -> Dict[str, Any]:
        dep = get_deployment(deployment_id)
        if not dep:
            raise DeploymentNotFound(f"Deployment {deployment_id} not found")

        status = dep.get("status")
        if status != DeployStatus.APPROVED.value:
            raise ValueError(
                f"Deployment {deployment_id} must be in APPROVED status to open a PR (current status: {status})"
            )

        rendered = dep.get("rendered") or {}
        if not rendered:
            raise ValueError(f"Deployment {deployment_id} has no rendered Terraform files")

        files_to_commit: Dict[str, Any] = {}
        for fname, content in rendered.items():
            path = f"{self.target_dir}/{fname}" if self.target_dir else fname
            files_to_commit[path] = content

        if self.add_workflows:
            files_to_commit[".github/workflows/terraagent-plan.yml"] = self._generate_plan_workflow()
            files_to_commit[".github/workflows/terraagent-apply.yml"] = self._generate_apply_workflow()

        source_name = dep.get("source_name") or deployment_id
        target_type = dep.get("target_type") or "AWS"
        title = f"[TerraAgent] Deploy {source_name} ({target_type})"
        body = self._generate_pr_body(dep)
        branch_name = f"terraagent/deploy-{deployment_id[-8:]}"

        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        async with httpx.AsyncClient(base_url=GITHUB_API_BASE, headers=headers, timeout=_REQUEST_TIMEOUT) as client:
            pr_data, commit_sha = await _publish(
                client,
                self.repo,
                from_branch=self.base_branch,
                branch_name=branch_name,
                files=files_to_commit,
                commit_message=f"feat(infra): deploy {source_name} to AWS via TerraAgent",
                pr={"title": title, "body": body, "base": self.base_branch},
            )

        pr_info = {
            "number": pr_data.get("number"),
            "html_url": pr_data.get("html_url"),
            "branch": branch_name,
            "repo": self.repo,
            "commit_sha": commit_sha,
            "base_branch": self.base_branch,
            "title": title,
            "created_at": pr_data.get("created_at"),
        }

        transition(
            deployment_id,
            DeployStatus.PR_OPEN,
            actor=dep.get("approved_by") or "gitops",
            reason=f"Opened GitHub PR #{pr_data.get('number')} on {self.repo}",
            pr=pr_info,
        )

        await redis_service.publish_log(
            deployment_id,
            f"[GITOPS] Pull Request #{pr_data.get('number')} opened: {pr_data.get('html_url')}",
            agent_name="deploy",
        )

        return {
            "status": DeployStatus.PR_OPEN.value,
            "pr_number": pr_data.get("number"),
            "pr_url": pr_data.get("html_url"),
            "branch": branch_name,
            "commit_sha": commit_sha,
        }

