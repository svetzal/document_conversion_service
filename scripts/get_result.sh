#!/usr/bin/env bash
set -euo pipefail

# Get job result (token-gated) and print markdown to stdout or save to file
# Usage: ./get_result.sh <job_id> <access_token> [output_file] [base_url]
# If output_file is '-', prints to stdout (default).
# Example: ./get_result.sh <job_id> <token> result.md http://localhost:8080

JOB_ID="${1:-}"
TOKEN="${2:-}"
OUTPUT_FILE="${3:--}"
BASE_URL="${4:-${BASE_URL:-http://localhost:8080}}"

if [[ -z "${JOB_ID}" || -z "${TOKEN}" ]]; then
  echo "Usage: $0 <job_id> <access_token> [output_file|-] [base_url]" >&2
  exit 1
fi

if [[ "${OUTPUT_FILE}" == "-" ]]; then
  curl -fsSL -H "Authorization: Bearer ${TOKEN}" \
    "${BASE_URL}/jobs/${JOB_ID}/result"
else
  curl -fsSL -H "Authorization: Bearer ${TOKEN}" \
    -o "${OUTPUT_FILE}" \
    "${BASE_URL}/jobs/${JOB_ID}/result"
  echo "Saved markdown to ${OUTPUT_FILE}" >&2
fi
