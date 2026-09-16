#!/usr/bin/env bash
set -e

echo "=== Running TerraAgent Verification Suite ==="

# 1. Check API Health
echo "Testing API Liveness..."
curl -s -f http://localhost:8000/api/health || { echo "API Health Check Failed"; exit 1; }
echo " API Healthy"

# 2. Check Nginx Ingress
echo "Testing Nginx Proxy..."
curl -s -f http://localhost/api/health || { echo "Nginx Proxy Failed"; exit 1; }
echo " Nginx Routing Healthy"

echo "=== All Core Tests Passed ==="
