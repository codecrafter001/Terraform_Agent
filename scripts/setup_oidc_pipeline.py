#!/usr/bin/env python3
"""
Setup helper script for TerraAgent AWS OIDC & GitHub Actions CI/CD Pipeline.

This script:
1. Validates AWS CLI credentials and region.
2. Initializes and applies the bootstrap Terraform module (infra/bootstrap-aws-oidc).
3. Reads the generated outputs (IAM Role ARN, S3 state bucket, DynamoDB lock table).
4. Automatically updates terraform/backend.tf with the real remote backend config.
5. Displays the GitHub Actions configuration instructions.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP_DIR = REPO_ROOT / "infra" / "bootstrap-aws-oidc"
TERRAFORM_DIR = REPO_ROOT / "terraform"


def run_cmd(cmd, cwd=None, capture=False):
    print(f"==> Running: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    res = subprocess.run(
        cmd,
        cwd=cwd or REPO_ROOT,
        shell=isinstance(cmd, str),
        text=True,
        capture_output=capture,
    )
    return res


def check_aws_auth():
    print("\n[1/5] Checking AWS CLI authentication...")
    res = run_cmd(["aws", "sts", "get-caller-identity"], capture=True)
    if res.returncode != 0:
        print("\n[!] Error: AWS credentials not found or invalid.")
        print("Please authenticate via AWS CLI first:")
        print("    aws configure")
        print("    OR")
        print("    aws sso login")
        return False
    try:
        identity = json.loads(res.stdout)
        print(f"[*] Authenticated as AWS Account: {identity.get('Account')} (ARN: {identity.get('Arn')})")
        return True
    except Exception:
        print("[*] AWS credentials found.")
        return True


def deploy_bootstrap(github_org: str, github_repo: str, aws_region: str):
    print(f"\n[2/5] Initializing & applying OIDC bootstrap Terraform in {BOOTSTRAP_DIR}...")
    
    init_res = run_cmd(["terraform", "init"], cwd=BOOTSTRAP_DIR)
    if init_res.returncode != 0:
        print("[!] terraform init failed.")
        return None

    apply_cmd = [
        "terraform", "apply", "-auto-approve",
        f"-var=github_org={github_org}",
        f"-var=github_repo={github_repo}",
        f"-var=aws_region={aws_region}",
    ]
    apply_res = run_cmd(apply_cmd, cwd=BOOTSTRAP_DIR)
    if apply_res.returncode != 0:
        print("[!] terraform apply failed.")
        return None

    # Fetch outputs
    output_res = run_cmd(["terraform", "output", "-json"], cwd=BOOTSTRAP_DIR, capture=True)
    if output_res.returncode != 0:
        print("[!] Failed to read terraform outputs.")
        return None

    outputs = json.loads(output_res.stdout)
    return {
        "role_arn": outputs.get("github_actions_role_arn", {}).get("value"),
        "state_bucket": outputs.get("tf_state_s3_bucket", {}).get("value"),
        "locks_table": outputs.get("tf_locks_dynamodb_table", {}).get("value"),
        "region": aws_region,
    }


def update_backend_tf(state_bucket: str, locks_table: str, region: str):
    print(f"\n[3/5] Updating {TERRAFORM_DIR / 'backend.tf'} with remote S3 backend configuration...")
    backend_content = f"""terraform {{
  required_version = ">= 1.5.0"
  required_providers {{
    aws = {{
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }}
  }}

  backend "s3" {{
    bucket         = "{state_bucket}"
    key            = "environments/production/terraform.tfstate"
    region         = "{region}"
    dynamodb_table = "{locks_table}"
    encrypt        = true
  }}
}}
"""
    backend_file = TERRAFORM_DIR / "backend.tf"
    with open(backend_file, "w", encoding="utf-8") as f:
        f.write(backend_content)
    print(f"[*] Successfully configured S3 backend in {backend_file}")


def print_summary(role_arn: str, state_bucket: str, locks_table: str, github_org: str, github_repo: str):
    print("\n" + "=" * 80)
    print("🚀 TERRAAGENT AWS STS OIDC & GITHUB ACTIONS SETUP COMPLETE")
    print("=" * 80)
    print(f"IAM Role ARN:       {role_arn}")
    print(f"S3 State Bucket:    {state_bucket}")
    print(f"DynamoDB Lock Table: {locks_table}")
    print(f"GitHub Repository:  {github_org}/{github_repo}")
    print("=" * 80)
    print("\nNext Steps:")
    print(f"1. In your GitHub repository (https://github.com/{github_org}/{github_repo}):")
    print("   Go to: Settings -> Secrets and variables -> Actions -> New repository secret")
    print("   Name:  AWS_DEPLOY_ROLE_ARN")
    print(f"   Value: {role_arn}")
    print("\n2. Commit and push the backend configuration:")
    print("   git add terraform/backend.tf")
    print("   git commit -m \"Configure S3 remote state backend for production\"")
    print("   git push origin feat/four-agent-restructure")
    print("\n3. Open a Pull Request on GitHub to test live PR Plan commenting and Merge Apply!")
    print("=" * 80 + "\n")


def main():
    print("=" * 80)
    print(" TerraAgent AWS OIDC & GitHub Actions CI/CD Automated Provisioner")
    print("=" * 80)

    github_org = os.getenv("GITHUB_ORG", "rashikagangraj")
    github_repo = os.getenv("GITHUB_REPO", "TerraAgent")
    aws_region = os.getenv("AWS_REGION", "us-east-1")

    if not check_aws_auth():
        sys.exit(1)

    result = deploy_bootstrap(github_org, github_repo, aws_region)
    if not result:
        sys.exit(1)

    update_backend_tf(result["state_bucket"], result["locks_table"], result["region"])
    print_summary(result["role_arn"], result["state_bucket"], result["locks_table"], github_org, github_repo)


if __name__ == "__main__":
    main()
