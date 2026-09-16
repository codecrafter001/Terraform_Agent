#!/usr/bin/env bash
set -e

REGION="${1:-us-east-1}"
OPERATION="${2:-generate}"

echo "=== Triggering TerraAgent Scan via REST API ==="
curl -s -X POST http://localhost:8000/api/scan \
  -H "Content-Type: application/json" \
  -d '{
    "aws_access_key": "'"${AWS_ACCESS_KEY_ID:-mock_key}"'",
    "aws_secret_key": "'"${AWS_SECRET_ACCESS_KEY:-mock_secret}"'",
    "region": "'"${REGION}"'",
    "operation": "'"${OPERATION}"'",
    "resource_filters": ["EC2", "VPC", "S3", "RDS", "IAM", "SG"]
  }' | jq .
