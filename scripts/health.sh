#!/usr/bin/env bash
set -euo pipefail

# Simple test for GET /health
# Usage: ./health.sh [base_url]
# Example: ./health.sh http://localhost:8080

BASE_URL="${1:-${BASE_URL:-http://localhost:8080}}"

curl -fsSL "${BASE_URL}/health" | jq -r '.'
