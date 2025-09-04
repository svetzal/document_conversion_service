import os
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, UploadFile, status, Header, HTTPException, Depends
from fastapi.responses import JSONResponse, PlainTextResponse
import uuid, base64, secrets

app = FastAPI(
    title="Document Conversion Service",
    version=os.getenv("DOC_SERVICE_VERSION", "0.1.0"),
    description=(
        "RESTful API for converting complex and proprietary documents into "
        "plain Markdown for LLM consumption."
    ),
)

# Global configuration defaults
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "300"))
ALLOWED_MIME = set(
    (os.getenv(
        "ALLOWED_MIME",
        "application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )).split(",")
)
DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
WORKERS = int(os.getenv("WORKERS", "4"))
JOB_TIMEOUT_SEC = int(os.getenv("JOB_TIMEOUT_SEC", "1800"))

# Async worker infrastructure
import asyncio
JOB_QUEUE: asyncio.Queue[str] = asyncio.Queue()
WORKER_TASKS: list[asyncio.Task] = []


def _new_capability_token() -> str:
    raw = secrets.token_bytes(32)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_to_bytes(token: str) -> bytes:
    # Add required padding for base64 urlsafe decoding
    pad = '=' * (-len(token) % 4)
    return base64.urlsafe_b64decode(token + pad)


def _argon2_hash_token(token: str) -> str:
    from argon2.low_level import Type, hash_secret

    raw = _b64url_to_bytes(token)
    salt = secrets.token_bytes(16)
    phc_bytes = hash_secret(
        secret=raw,
        salt=salt,
        time_cost=3,
        memory_cost=65536,
        parallelism=1,
        hash_len=32,
        type=Type.ID,
    )
    # hash_secret returns bytes (PHC string); decode to str
    return phc_bytes.decode("utf-8")


@app.get("/hello")
def hello() -> dict[str, str]:
    return {"message": "hello world"}


@app.get("/health")
def health() -> dict[str, str]:
    """Basic health check endpoint."""
    return {"status": "ok"}


def _job_dir(job_id: str) -> Path:
    return DATA_DIR / "jobs" / job_id


def _load_job(job_id: str) -> dict[str, object]:
    job_path = _job_dir(job_id) / "job.json"
    if not job_path.exists():
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "job not found"})
    with job_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _save_job(job: dict[str, object]) -> None:
    job_id = str(job["id"])  # type: ignore[index]
    job_path = _job_dir(job_id) / "job.json"
    job_path.parent.mkdir(parents=True, exist_ok=True)
    with job_path.open("w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=2)


def _validate_bearer_token(auth_header: str | None) -> str:
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "missing bearer token"})
    token = auth_header.removeprefix("Bearer ").strip()
    # Strict regex: 43 chars base64url unpadded
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "malformed token"})
    return token


def _verify_token(job: dict[str, object], token: str) -> None:
    from argon2.low_level import verify_secret
    raw = _b64url_to_bytes(token)
    phc = str(job.get("access_token_hash", ""))
    try:
        ok = verify_secret(phc.encode("utf-8"), raw)
    except Exception:
        ok = False
    if not ok:
        # 403 when token is well-formed but invalid per PLAN
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "invalid token"})


async def _worker_loop(name: str) -> None:
    while True:
        job_id = await JOB_QUEUE.get()
        try:
            job = _load_job(job_id)
            # mark running
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            job["status"] = "running"
            job["started_at"] = now
            job["updated_at"] = now
            _save_job(job)

            # Actual conversion using Docling
            input_uri = str(job["input_uri"])  # type: ignore[index]
            output_dir = _job_dir(job_id) / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / "result.md"

            async def convert_to_markdown() -> str:
                # Run blocking docling pipeline in a thread
                def _run() -> str:
                    # Import pipeline with compatibility across docling versions
                    from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline
                    from docling.datamodel.base_models import InputFormat
                    from docling_core.types.doc import DoclingDocument

                    # Preferred path (per Docling README): use DocumentConverter when available
                    try:
                        from docling.document_converter import DocumentConverter  # type: ignore
                        converter = DocumentConverter()
                        result = converter.convert(input_uri)
                        # Extract document across possible result shapes
                        doc: DoclingDocument | None = None
                        if hasattr(result, "document"):
                            doc = result.document  # type: ignore[attr-defined]
                        elif hasattr(result, "to_doc") and callable(getattr(result, "to_doc")):
                            doc = result.to_doc()  # type: ignore[assignment]
                        elif isinstance(result, DoclingDocument):
                            doc = result
                        else:
                            raise RuntimeError("Unexpected result type from DocumentConverter; cannot extract document")
                        # Convert to Markdown (method names may vary)
                        if hasattr(doc, "export_to_markdown"):
                            return doc.export_to_markdown()
                        if hasattr(doc, "to_markdown"):
                            return doc.to_markdown()  # type: ignore[no-any-return]
                        if hasattr(doc, "as_markdown"):
                            return doc.as_markdown()  # type: ignore[no-any-return]
                        raise RuntimeError("Doc object from DocumentConverter lacks a markdown export method")
                    except Exception:
                        pass

                    # Try to construct pipeline options in a version-tolerant way.
                    pipe = None
                    # Strategy 1: Options class in same module
                    try:
                        from docling.pipeline.standard_pdf_pipeline import StandardPdfPipelineOptions  # type: ignore
                        opts = StandardPdfPipelineOptions()  # default options
                        pipe = StandardPdfPipeline(pipeline_options=opts)
                    except Exception:
                        pass

                    # Strategy 2: get_default_options classmethod on the pipeline
                    if pipe is None:
                        try:
                            get_opts = getattr(StandardPdfPipeline, "get_default_options", None)
                            if callable(get_opts):
                                opts = get_opts()
                                pipe = StandardPdfPipeline(pipeline_options=opts)
                        except Exception:
                            pass

                    # Strategy 3: alternate module for options (some versions)
                    if pipe is None:
                        try:
                            from docling.pipeline.standard_pdf_pipeline_options import StandardPdfPipelineOptions as StdPdfOpts  # type: ignore
                            opts = StdPdfOpts()
                            pipe = StandardPdfPipeline(pipeline_options=opts)
                        except Exception:
                            pass

                    # Strategy 4: fall back to no-arg init (older versions accepted this)
                    if pipe is None:
                        try:
                            pipe = StandardPdfPipeline()
                        except Exception as e:
                            # Re-raise with a clearer message; it will be captured into job["error"]
                            raise RuntimeError("Docling StandardPdfPipeline initialization failed across known API variants. Please ensure a compatible docling version is installed.") from e
                    # Detect input format based on extension; fallback to PDF handling which is most common
                    suffix = Path(input_uri).suffix.lower()
                    fmt = InputFormat.from_suffix(suffix) if hasattr(InputFormat, 'from_suffix') else None
                    # Pipeline currently expects PDF paths; for non-PDF, attempt generic load when supported
                    # Many formats are supported in docling via same pipeline when dependencies are present.
                    # Execute pipeline across API variants
                    run_methods = [
                        "run",               # older versions
                        "run_pdf",           # hypothetical variant
                        "process",           # generic name in some libs
                        "__call__",          # callable pipeline
                    ]
                    result = None
                    last_err = None
                    for m in run_methods:
                        fn = getattr(pipe, m, None)
                        if callable(fn):
                            try:
                                result = fn(input_uri)
                                break
                            except Exception as e:
                                last_err = e
                    if result is None:
                        # Try a standardized runner on the module if provided
                        try:
                            from docling.pipeline.standard_pdf_pipeline import run_pipeline  # type: ignore
                            result = run_pipeline(pipe, input_uri)
                        except Exception as e:
                            if last_err is None:
                                last_err = e
                    if result is None:
                        raise RuntimeError("Docling pipeline does not expose a usable run/process method on this version.") from last_err

                    # Extract document across possible result shapes
                    doc: DoclingDocument | None = None
                    if hasattr(result, "document"):
                        doc = result.document  # type: ignore[attr-defined]
                    elif hasattr(result, "to_doc") and callable(getattr(result, "to_doc")):
                        doc = result.to_doc()  # type: ignore[assignment]
                    elif isinstance(result, DoclingDocument):
                        doc = result
                    else:
                        raise RuntimeError("Unexpected result type from Docling pipeline; cannot extract document")

                    # Convert to Markdown (method names may vary)
                    if hasattr(doc, "export_to_markdown"):
                        return doc.export_to_markdown()
                    if hasattr(doc, "to_markdown"):
                        return doc.to_markdown()  # type: ignore[no-any-return]
                    if hasattr(doc, "as_markdown"):
                        return doc.as_markdown()  # type: ignore[no-any-return]
                    raise RuntimeError("Doc object does not provide a markdown export method across known variants")
                return await asyncio.to_thread(_run)

            md = await convert_to_markdown()

            # Write output markdown (offloaded to thread)
            def write_output() -> None:
                with output_path.open("w", encoding="utf-8") as f:
                    f.write(md)

            await asyncio.to_thread(write_output)

            # update job
            now2 = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            job["progress"] = 100
            job["output_uri"] = str(output_path)
            job["status"] = "succeeded"
            job["completed_at"] = now2
            job["updated_at"] = now2
            _save_job(job)
        except Exception as e:
            job = None
            try:
                j = _load_job(job_id)
                j["status"] = "failed"
                j["error"] = str(e)
                j["failed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                j["updated_at"] = j["failed_at"]
                _save_job(j)
            except Exception:
                pass
        finally:
            JOB_QUEUE.task_done()


@app.on_event("startup")
async def _startup() -> None:
    # Ensure base directories
    (DATA_DIR / "jobs").mkdir(parents=True, exist_ok=True)
    # Start workers
    for i in range(WORKERS):
        task = asyncio.create_task(_worker_loop(f"worker-{i+1}"))
        WORKER_TASKS.append(task)


@app.on_event("shutdown")
async def _shutdown() -> None:
    for t in WORKER_TASKS:
        t.cancel()


@app.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
async def create_job(file: UploadFile = File(...), authorization: str | None = Header(None)) -> JSONResponse:
    """Create a new conversion job from an uploaded document.

    Accepts multipart/form-data with a single required part named "file".
    Persists the input and per-job metadata JSON under DATA_DIR/jobs/{job_id}/.
    Returns 202 Accepted with a newly created job id and a one-time access_token.
    """
    job_id = str(uuid.uuid4())
    token = _new_capability_token()
    token_hash = _argon2_hash_token(token)

    # Resolve data paths
    data_dir = Path(os.getenv("DATA_DIR", "./data")).resolve()
    job_dir = data_dir / "jobs" / job_id
    input_dir = job_dir / "input"
    output_dir = job_dir / "output"
    artifacts_dir = job_dir / "artifacts"
    for d in (input_dir, output_dir, artifacts_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Determine a safe filename and extension; preserve extension if present
    original_name = file.filename or "upload"
    # Extract extension safely
    ext = ""
    if "." in original_name:
        # keep the last suffix only
        ext = "." + original_name.rsplit(".", 1)[-1]
    safe_base = "original"
    input_path = input_dir / f"{safe_base}{ext}"

    # Validate content type (if provided)
    if file.content_type and ALLOWED_MIME and file.content_type not in ALLOWED_MIME:
        raise HTTPException(status_code=415, detail={"code": "unsupported_media_type", "message": f"content-type {file.content_type} not allowed"})

    # Stream file to disk and compute SHA-256 checksum and size with limit
    sha256 = hashlib.sha256()
    size_bytes = 0
    CHUNK = 1024 * 1024
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    with input_path.open("wb") as f_out:
        while True:
            chunk = await file.read(CHUNK)
            if not chunk:
                break
            size_bytes += len(chunk)
            if size_bytes > max_bytes:
                # Stop reading and clean up
                f_out.close()
                try:
                    input_path.unlink(missing_ok=True)
                except Exception:
                    pass
                raise HTTPException(status_code=413, detail={"code": "payload_too_large", "message": f"upload exceeds {MAX_UPLOAD_MB} MB"})
            f_out.write(chunk)
            sha256.update(chunk)

    checksum_hex = sha256.hexdigest()

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # Build job metadata per PLAN §3
    job_meta: dict[str, object] = {
        "id": job_id,
        "filename": original_name,
        "content_type": file.content_type or "application/octet-stream",
        "size_bytes": size_bytes,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "completed_at": None,
        "failed_at": None,
        "status": "queued",
        "progress": 0,
        "error": None,
        "input_uri": str(input_path),
        "output_uri": None,
        "artifacts": [],
        "checksum": checksum_hex,
        "access_token_hash": token_hash,
    }

    # Persist job.json
    with (job_dir / "job.json").open("w", encoding="utf-8") as f:
        json.dump(job_meta, f, ensure_ascii=False, indent=2)

    body = {
        "id": job_id,
        "status": "queued",
        "progress": 0,
        "access_token": token,  # shown once; do not log
        "links": {
            "self": f"/jobs/{job_id}",
            "result": f"/jobs/{job_id}/result",
        },
    }
    # Enqueue job for async processing
    await JOB_QUEUE.put(job_id)

    headers = {"Location": f"/jobs/{job_id}"}
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=body, headers=headers)


@app.get("/jobs/{job_id}")
async def get_job(job_id: str, authorization: str | None = Header(None)) -> JSONResponse:
    token = _validate_bearer_token(authorization)
    job = _load_job(job_id)
    _verify_token(job, token)
    # Do not expose token hash in response
    redacted = {k: v for k, v in job.items() if k != "access_token_hash"}
    return JSONResponse(content=redacted)


@app.get("/jobs/{job_id}/result", response_class=PlainTextResponse)
async def get_result(job_id: str, authorization: str | None = Header(None)) -> PlainTextResponse:
    token = _validate_bearer_token(authorization)
    job = _load_job(job_id)
    _verify_token(job, token)
    output_uri = job.get("output_uri")
    if not output_uri or not Path(str(output_uri)).exists():
        raise HTTPException(status_code=404, detail={"code": "not_ready", "message": "result not available"})
    with Path(str(output_uri)).open("r", encoding="utf-8") as f:
        content = f.read()
    return PlainTextResponse(content=content, media_type="text/markdown")


def run() -> None:
    """Run a development ASGI server using uvicorn.

    Exposes the app at host:port (default 0.0.0.0:8080). Set PORT env var to override.
    """
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    # Enable reload in dev unless explicitly disabled
    reload = os.getenv("RELOAD", "true").lower() in {"1", "true", "yes", "on"}

    uvicorn.run("doc_service.main:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    run()
