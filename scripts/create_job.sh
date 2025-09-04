#!/usr/bin/env bash
set -euo pipefail

# Create a conversion job by uploading a file to POST /jobs
# Requires: jq
# Usage: ./create_job.sh <file_path> [base_url]
# Example: ./create_job.sh ./sample.pdf http://localhost:8080

if ! command -v jq >/dev/null 2>&1; then
  echo "Error: jq is required but not installed." >&2
  exit 1
fi

FILE_PATH="${1:-}"
if [[ -z "${FILE_PATH}" ]]; then
  echo "Usage: $0 <file_path> [base_url]" >&2
  exit 1
fi

BASE_URL="${2:-${BASE_URL:-http://localhost:8080}}"

RESP=$(curl -fsSL -X POST \
  -F "file=@${FILE_PATH}" \
  "${BASE_URL}/jobs")

# Print raw response and extracted values
echo "Response:" >&2
echo "${RESP}" | jq -r '.' >&2

JOB_ID=$(echo "${RESP}" | jq -r '.id')
ACCESS_TOKEN=$(echo "${RESP}" | jq -r '.access_token')

if [[ -z "${JOB_ID}" || -z "${ACCESS_TOKEN}" || "${JOB_ID}" == "null" || "${ACCESS_TOKEN}" == "null" ]]; then
  echo "Failed to parse job id or access token from response" >&2
  exit 1
fi

echo "JOB_ID=${JOB_ID}"
echo "ACCESS_TOKEN=${ACCESS_TOKEN}"
