#!/usr/bin/env bash
set -euo pipefail

# Get job details (token-gated)
# Requires: jq
# Usage: ./get_job.sh <job_id> <access_token> [base_url]
# Example: ./get_job.sh 123e4567-e89b-12d3-a456-426614174000 <token> http://localhost:8080

if ! command -v jq >/dev/null 2>&1; then
  echo "Error: jq is required but not installed." >&2
  exit 1
fi

JOB_ID="${1:-}"
TOKEN="${2:-}"
if [[ -z "${JOB_ID}" || -z "${TOKEN}" ]]; then
  echo "Usage: $0 <job_id> <access_token> [base_url]" >&2
  exit 1
fi

BASE_URL="${3:-${BASE_URL:-http://localhost:8080}}"

curl -fsSL -H "Authorization: Bearer ${TOKEN}" \
  "${BASE_URL}/jobs/${JOB_ID}" | jq -r '.'
