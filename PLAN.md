# Document Conversion Service — MVP Plan (Polling + asyncio)

Last updated: 2025-09-04

This document defines a minimal, pragmatic MVP for an asynchronous document conversion service that accepts large documents (PDF, Word, PowerPoint), processes them in the background using asyncio for parallelism, and exposes endpoints to check status (via polling) and download results.


## 1) Scope and Principles

- Focus on a single-instance MVP using FastAPI with in-process asyncio workers.
- Polling endpoint for job status.
- Per-job capability tokens for access to job status and results.
- Local filesystem storage for inputs and outputs.
- Python 3.13+ typing conventions: builtin generics, modern union syntax; avoid Any.


## 2) Minimal Architecture

- FastAPI app exposing endpoints for job creation, status, and result download.
- Background processing via asyncio:
  - In-memory asyncio.Queue for submitted jobs.
  - A bounded worker pool (asyncio tasks) consumes the queue.
  - CPU-heavy conversion is offloaded to a ThreadPoolExecutor (default 4 workers).
  - Each job runs in its own task; update progress into the job store.
- Storage: ./data/jobs/{job_id}/ for inputs, outputs, and small artifacts.
- Job metadata persistence: per-job JSON files are persisted under data/ by default for durability.


## 3) Job Model (MVP)

- id: str (UUID v4)
- filename: str
- content_type: str
- size_bytes: int
- created_at, updated_at, started_at, completed_at, failed_at: ISO8601 strings
- status: "queued" | "running" | "succeeded" | "failed"
- progress: int (0–100)
- error: str | None
- input_uri: str (local path)
- output_uri: str | None (local path to produced .md)
- artifacts: list[str]
- checksum: str | None (SHA-256 of input)
- access_token_hash: str (Argon2id hash of the one-time capability token, stored as PHC string)

Notes:
- Only the token hash is stored. The raw token is returned once at job creation.
- Constant-time compare for presented tokens.


## 4) API Surface

Existing:
- GET /health -> {"status": "ok"}

New endpoints (all capability-token gated per job):
- POST /jobs
  - Purpose: Accept a document upload and create an async conversion job.
  - Request: multipart/form-data with a single required file part named "file".
    - The uploaded document is sent as the value of the "file" part.
  - Response 202: Job summary, access_token (shown once), and capability links
    - Location: /jobs/{id}
    - links.self, links.result do NOT embed tokens; clients must send Authorization: Bearer <token>.
- GET /jobs/{id}
  - Requires Authorization: Bearer <token>
  - Returns job detail (no token fields)
- GET /jobs/{id}/result
  - Requires Authorization: Bearer <token>; returns text/markdown (or 404 if not ready)



## 5) Async Worker Design (asyncio)

- Use a single asyncio.Queue[job_id] shared across workers.
- Start a fixed-size worker pool on app startup (WORKERS env, default 4).
- Worker isolation for conversion: offload CPU-heavy conversion to a ThreadPoolExecutor with max_workers=4 by default (configurable via WORKERS). Use asyncio.to_thread to dispatch DoclingConverter.convert blocking parts.
- Each worker loop:
  1) get job_id from queue
  2) load job metadata
  3) mark running, set started_at
  4) run DoclingConverter.convert(...) via the thread pool and update progress milestones
  5) write output Markdown to output_uri
  6) mark succeeded with completed_at, or failed with error
- Ensure cooperative cancellation and timeouts per job (CONFIG: JOB_TIMEOUT_SEC). On cancellation, cancel any pending tasks and avoid leaking threads.
- Use an asyncio.Semaphore to bound any internal concurrency if needed (e.g., I/O bursts).


## 6) Storage Layout (local only)

- DATA_DIR env var (default ./data)
- Paths:
  - data/jobs/{job_id}/input/original.ext
  - data/jobs/{job_id}/output/result.md
  - data/jobs/{job_id}/artifacts/
- Metadata: per-job JSON file persisted at data/jobs/{job_id}/job.json after each update


## 7) Security (Capability Tokens)

- Representation: base64url (RFC 4648 URL-safe), unpadded.
- Generation: 256-bit random bytes (secrets.token_bytes(32)) -> base64url, strip any '=' padding (43 chars).
- Validation (strict): accept only canonical unpadded base64url: regex `^[A-Za-z0-9_-]{43}$`.
- Transmission: Authorization: Bearer <token>.
- Storage: store a hash of the raw token bytes using Argon2id only. Store as PHC string (e.g., $argon2id$v=19$m=65536,t=3,p=1$...). Never persist the raw token.
- One-time disclosure: return the raw token exactly once in POST /jobs response.
- Verification: normalize padding if needed, decode to raw bytes, verify with Argon2id (constant-time under the hood).
- Authorization errors: 401 for missing/malformed token; 403 for well-formed but incorrect token (constant-time compare).


## 8) Errors and Limits

- Standard JSON error: { code: str, message: str, details?: dict[str, str | int | float | bool] }
- Enforce max upload size (MAX_UPLOAD_MB env; reject > limit with 413).
- Validate allowed MIME types (ALLOWED_MIME env, comma-separated).
- Idempotency-Key header is planned to de-duplicate POST /jobs in client retries (MVP target).


## 9) Configuration (env)

- HOST, PORT (already supported)
- DATA_DIR (default ./data)
- MAX_UPLOAD_MB (default 300)
- ALLOWED_MIME (default: application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.openxmlformats-officedocument.presentationml.presentation)
- WORKERS (default 4)  # also used as ThreadPoolExecutor max_workers for conversion
- JOB_TIMEOUT_SEC (default 1800)


## 10) Implementation Steps

1) Define Pydantic models and enums for Job and responses (create/detail).  
   - Status: Deferred for now; using dict-based models in code. Will introduce Pydantic in a later iteration.
2) Implement token generation, hashing, and constant-time verification. ✓  
   - Implemented in src/doc_service/main.py: _new_capability_token (lines 38–41), _b64url_to_bytes (43–47), _argon2_hash_token (49–65), _validate_bearer_token (98–106), _verify_token (109–120). Uses Argon2id via argon2.low_level and enforces strict 43-char base64url tokens with 401/403 split.
3) Implement job store that persists per-job JSON (job.json) under DATA_DIR/jobs/{job_id}/. ✓  
   - Implemented via _load_job (82–88), _save_job (90–96); POST /jobs persists job.json (394–397) under DATA_DIR/jobs/{job_id}/.
4) Implement POST /jobs (multipart) that returns access_token once and capability links. ✓  
   - Implemented at create_job (312–413): validates size and MIME, streams to disk, computes checksum, persists metadata, enqueues job, returns 202 with Location, links, and one-time access_token.
5) Implement GET /jobs/{id} and GET /jobs/{id}/result (both require token). ✓  
   - Implemented at get_job (415–423) and get_result (425–435) with Bearer token validation and proper redaction/content types.
6) Implement asyncio worker pool and queue; integrate DoclingConverter to perform conversion and report progress. ✓  
   - Implemented: JOB_QUEUE/WORKER_TASKS (33–36), _worker_loop (122–294) with docling conversion and output writing; progress/status updates.
7) Wire startup and shutdown events to init/stop workers cleanly. ✓  
   - Implemented at @app.on_event("startup") (296–304) and @app.on_event("shutdown") (306–310).
8) Add basic validation, limits, and structured error responses. ✓  
   - Implemented: MAX_UPLOAD_MB limit with 413 (351–366), ALLOWED_MIME with 415 (344–346), consistent JSON error bodies, and 404/401/403 handling.


## 11) Example Interaction

- Create job:
  POST /jobs (multipart)
  -> 202 Accepted with id, status=queued, progress=0, access_token (once), links.self/result (no tokens embedded)

- Poll status:
  GET /jobs/{id}
  Authorization: Bearer <base64url-token>
  -> { id, status: running, progress: 65, updated_at, error: null }

- Download result:
  GET /jobs/{id}/result
  Authorization: Bearer <base64url-token>
  -> text/markdown



---
This MVP plan is intentionally minimal: asyncio for parallel processing, polling for job status, per-job capability tokens, and local storage.



## 12) Code Design: Swappable Abstractions

Goal: Keep conversion and storage behind minimal interfaces so implementations can be replaced without changing endpoints or business logic.

- Interfaces (Protocols)
  - Storage
    - save_input(job_id: str, filename: str, data: Iterable[bytes]) -> str
    - save_output(job_id: str, content: str) -> str
    - result_exists(job_id: str) -> bool
    - open_result(job_id: str) -> Iterable[bytes]
    - list_artifacts(job_id: str) -> list[str]
  - Converter
    - async convert(input_uri: str, report_progress: callable[[int, str | None], None] | None = None) -> tuple[str, list[str]]
      - Returns: (markdown_content, artifacts) where artifacts are paths/URIs written by Storage

- Default implementations (MVP)
  - LocalFileStorage: stores inputs/outputs under DATA_DIR (see “Storage Layout”).
  - DoclingConverter: uses the docling engine to parse supported formats and produce Markdown.

- Wiring
  - On application startup, the app constructs concrete instances:
    - storage = LocalFileStorage(DATA_DIR)
    - converter = DoclingConverter()
  - The worker receives these via dependency injection (constructor args or module-level singletons limited to app lifecycle).

- Swap strategy
  - To use remote storage (e.g., object store) implement Storage with the same methods (e.g., S3Storage). No changes required in routes or worker logic.
  - To swap conversion engines, implement another Converter (e.g., AltConverter) with the same signature. The worker calls Converter.convert; progress updates and outputs remain unchanged from the worker’s perspective.

- Typing conventions
  - Use Python 3.13 builtin generics and modern union syntax as used across the plan.


## 13) Audit Findings and Open Questions

This section was added as part of the PLAN.md audit on 2025-09-04. It captures discrepancies, risks, and decisions needed before implementation proceeds.

- Endpoint discrepancy vs guidelines — Resolved ✓
  - We no longer expose GET /hello. The only basic liveness endpoint is GET /health.

- Package name vs guidelines
  - Guidelines: project package name should be "document-conversion-service". Current pyproject [project.name] is "doc-service". Question: Do we rename the distribution to "document-conversion-service" (keeping the CLI name "doc-service"), or keep the shorter name?

- Unexpected dependency
  - pyproject lists a dependency "mojentic" which is not referenced in PLAN or code. Question: Is this intentional? If not, we'll remove it from dependencies to keep the footprint minimal.

- Docling dependency timing
  - Guidelines say Docling is a future/planned engine. pyproject declares "docling" as a dependency now. Question: Keep it now (to speed up MVP integration) or defer until conversion is implemented? If kept, please confirm minimal supported version and extras.

- Token hashing library
  - PLAN specifies Argon2id hashing but not the library. Proposal: use argon2-cffi for hashing/verification (widely used, PHC-compliant). Question: Approve argon2-cffi, or prefer an alternative (e.g., passlib with argon2 backend, or libsodium)?

- Idempotency-Key support scope
  - PLAN now marks Idempotency-Key as "planned". Question: Confirm intended semantics: header name ("Idempotency-Key"), storage duration/window, key length limits, and whether scope is per endpoint per user or global per project.

- Error schema details
  - PLAN specifies a standard JSON error with fields {code, message, details?}. Question: Include a request_id/correlation_id? Do we standardize HTTP-to-code mapping (e.g., ValidationError -> "validation_error")?

- Allowed MIME types and formats
  - PLAN default allows PDF, DOCX, PPTX. Question: Include XLSX in MVP? If yes, add MIME type application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.

- Worker and thread pool sizing
  - PLAN uses WORKERS for both asyncio worker count and ThreadPoolExecutor max_workers. This can oversubscribe CPU-bound work. Question: Keep a single knob for simplicity, or split into WORKERS (async) and CPU_WORKERS (threads)?

- Security token format/length
  - PLAN mandates 256-bit tokens as unpadded base64url with length 43. Current code uses secrets.token_bytes(32) and strips padding, which yields 43 chars. No action needed; just confirming.

- Access token hash lifecycle
  - PLAN updated to allow access_token_hash to be null before Argon2 is integrated. Once hashing is added, do we require backfilling existing jobs, or leave historical jobs with null hashes?

- Error and limits — Implemented ✓
  - MAX_UPLOAD_MB, ALLOWED_MIME validation, structured errors, and 413 handling are implemented in src/doc_service/main.py (see lines 344–346, 351–366, and error responses across endpoints).

- Build configuration (Hatch)
  - [tool.hatch.build.targets.wheel] currently lists packages = ["src/doc_service"]. Hatch typically expects packages specified as import names (e.g., ["doc_service"]). Question: Confirm packaging settings to avoid missing files in built wheels.

- Minor copy edit in code (not a plan blocker)
  - src/doc_service/main.py description string says "plan Markdown" instead of "plain Markdown".

Please provide decisions on the questions above; I will then update the plan and/or code accordingly before implementation begins.
