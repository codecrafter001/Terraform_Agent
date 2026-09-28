# TerraAgent AWS OIDC & GitHub Actions Pipeline Setup for Windows PowerShell

param (
    [string]$GithubOrg = "codecrafter001",
    [string]$GithubRepo = "Terraform_Agent",
    [string]$AwsRegion = "us-east-1"
)

$ErrorActionPreference = "Stop"

Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host " TerraAgent AWS STS OIDC & GitHub Actions Pipeline Setup" -ForegroundColor Cyan
Write-Host "=================================================================" -ForegroundColor Cyan

# 1. Check AWS Credentials
Write-Host "`n[1/4] Checking AWS CLI authentication..." -ForegroundColor Yellow
try {
    $identityJson = aws sts get-caller-identity
    $identity = $identityJson | ConvertFrom-Json
    Write-Host "[*] Authenticated to AWS Account: $($identity.Account) ($($identity.Arn))" -ForegroundColor Green
} catch {
    Write-Host "[!] AWS CLI is not authenticated. Please run 'aws configure' first." -ForegroundColor Red
    exit 1
}

# 2. Terraform Init & Apply in infra/bootstrap-aws-oidc
$bootstrapDir = Join-Path $PSScriptRoot "..\infra\bootstrap-aws-oidc"
Write-Host "`n[2/4] Deploying OIDC IAM Role and S3/DynamoDB Backend in $bootstrapDir..." -ForegroundColor Yellow

Push-Location $bootstrapDir
try {
    terraform init
    terraform apply -auto-approve -var="github_org=$GithubOrg" -var="github_repo=$GithubRepo" -var="aws_region=$AwsRegion"
    
    $outputsJson = terraform output -json
    $outputs = $outputsJson | ConvertFrom-Json
    $roleArn = $outputs.github_actions_role_arn.value
    $stateBucket = $outputs.tf_state_s3_bucket.value
    $locksTable = $outputs.tf_locks_dynamodb_table.value
} finally {
    Pop-Location
}

# 3. Update terraform/backend.tf
Write-Host "`n[3/4] Updating terraform/backend.tf with remote state configuration..." -ForegroundColor Yellow
$backendFile = Join-Path $PSScriptRoot "..\terraform\backend.tf"
$backendContent = @"
terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket         = "$stateBucket"
    key            = "environments/production/terraform.tfstate"
    region         = "$AwsRegion"
    dynamodb_table = "$locksTable"
    encrypt        = true
  }
}
"@

Set-Content -Path $backendFile -Value $backendContent -Encoding utf8
Write-Host "[*] Successfully updated $backendFile" -ForegroundColor Green

# 4. Display Next Steps
Write-Host "`n=================================================================" -ForegroundColor Cyan
Write-Host "🎉 PROVISIONING SUCCESSFUL!" -ForegroundColor Green
Write-Host "=================================================================" -ForegroundColor Cyan
Write-Host "IAM Role ARN:       $roleArn" -ForegroundColor Yellow
Write-Host "S3 State Bucket:    $stateBucket" -ForegroundColor Yellow
Write-Host "DynamoDB Lock Table:$locksTable" -ForegroundColor Yellow
Write-Host "`nNext Step: Go to https://github.com/$GithubOrg/$GithubRepo/settings/secrets/actions"
Write-Host "Add Secret Name:  AWS_DEPLOY_ROLE_ARN"
Write-Host "Add Secret Value: $roleArn"
Write-Host "=================================================================" -ForegroundColor Cyan
