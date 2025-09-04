#!/usr/bin/env bash
set -euo pipefail

# End-to-end demo flow:
# 1) Create job with a given file
# 2) Poll status until succeeded or failed
# 3) Fetch result when ready
# Requires: jq
# Usage: ./demo_flow.sh <file_path> [base_url]

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

# 1) Create job
RESP=$(curl -fsSL -X POST \
  -F "file=@${FILE_PATH}" \
  "${BASE_URL}/jobs")
JOB_ID=$(echo "${RESP}" | jq -r '.id')
TOKEN=$(echo "${RESP}" | jq -r '.access_token')
if [[ -z "${JOB_ID}" || -z "${TOKEN}" || "${JOB_ID}" == "null" || "${TOKEN}" == "null" ]]; then
  echo "Failed to create job" >&2
  echo "Response: ${RESP}" >&2
  exit 1
fi

echo "Created job: ${JOB_ID}" >&2

# 2) Poll until done
STATUS=""
ATTEMPTS=0
MAX_ATTEMPTS=${MAX_ATTEMPTS:-60}
SLEEP_SEC=${SLEEP_SEC:-1}

while (( ATTEMPTS < MAX_ATTEMPTS )); do
  ATTEMPTS=$((ATTEMPTS+1))
  JR=$(curl -fsSL -H "Authorization: Bearer ${TOKEN}" "${BASE_URL}/jobs/${JOB_ID}")
  STATUS=$(echo "${JR}" | jq -r '.status')
  PROGRESS=$(echo "${JR}" | jq -r '.progress')
  echo "status=${STATUS} progress=${PROGRESS}% (attempt ${ATTEMPTS}/${MAX_ATTEMPTS})" >&2
  if [[ "${STATUS}" == "succeeded" ]]; then
    break
  fi
  if [[ "${STATUS}" == "failed" ]]; then
    echo "Job failed:" >&2
    echo "${JR}" | jq -r '.' >&2
    exit 2
  fi
  sleep "${SLEEP_SEC}"
done

if [[ "${STATUS}" != "succeeded" ]]; then
  echo "Timed out waiting for job to complete" >&2
  exit 3
fi

# 3) Fetch result
curl -fsSL -H "Authorization: Bearer ${TOKEN}" \
  "${BASE_URL}/jobs/${JOB_ID}/result"
