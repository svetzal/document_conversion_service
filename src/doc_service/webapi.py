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
        ",".join([
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # docx
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # pptx
            "application/vnd.openxmlformats-officedocument.presentationml.slideshow",  # ppsx
            "application/vnd.ms-powerpoint",  # legacy .ppt
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # xlsx
        ]),
    )).split(",")
)
DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
WORKERS = int(os.getenv("WORKERS", "4"))
JOB_TIMEOUT_SEC = int(os.getenv("JOB_TIMEOUT_SEC", "1800"))

# Async worker infrastructure delegated to domain service
import asyncio
try:
    from doc_service.conversion.adapters import LocalStorage, Argon2Security, DoclingConverter
    from doc_service.conversion import ConversionService, JobRecord
except ImportError:
    # Allow running as a script: `python src/doc_service/webapi.py`
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.append(str(_Path(__file__).resolve().parents[2]))  # add ./src to sys.path
    from doc_service.conversion.adapters import LocalStorage, Argon2Security, DoclingConverter
    from doc_service.conversion import ConversionService, JobRecord

SERVICE: ConversionService | None = None


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




@app.on_event("startup")
async def _startup() -> None:
    # Ensure base directories
    (DATA_DIR / "jobs").mkdir(parents=True, exist_ok=True)
    # Initialize domain service and start workers
    global SERVICE
    storage = LocalStorage(str(DATA_DIR))
    security = Argon2Security()
    converter = DoclingConverter()
    SERVICE = ConversionService(storage=storage, security=security, converter=converter, workers=WORKERS)
    await SERVICE.start()


@app.on_event("shutdown")
async def _shutdown() -> None:
    global SERVICE
    if SERVICE is not None:
        await SERVICE.stop()


@app.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
async def create_job(file: UploadFile = File(...), authorization: str | None = Header(None)) -> JSONResponse:
    """Create a new conversion job from an uploaded document.

    Accepts multipart/form-data with a single required part named "file".
    Persists the input and per-job metadata JSON under DATA_DIR/jobs/{job_id}/.
    Returns 202 Accepted with a newly created job id and a one-time access_token.
    """
    # Validate media type: allow known MIME types; if client sends a non-standard
    # content-type but the filename extension is supported, accept it to avoid 415.
    ct = (file.content_type or "").strip().lower()
    fn = (file.filename or "").lower()
    # Expand supported extensions here to tolerate odd client MIME labels
    supported_exts = {".pdf", ".docx", ".pptx", ".ppsx", ".ppt", ".xlsx"}
    def _has_supported_ext(name: str) -> bool:
        import os as _os
        _, ext = _os.path.splitext(name)
        return ext in supported_exts
    if ct and ALLOWED_MIME and ct not in ALLOWED_MIME:
        # Accept common legacy/alternate PPT MIME types by default
        ppt_variants = {
            "application/vnd.ms-powerpoint",  # .ppt
            "application/vnd.openxmlformats-officedocument.presentationml.slideshow",  # .ppsx
            "application/octet-stream",  # some clients default to this
        }
        if ct in ppt_variants and _has_supported_ext(fn):
            pass
        elif _has_supported_ext(fn):
            # If extension is supported, let the domain layer handle it
            pass
        else:
            raise HTTPException(status_code=415, detail={"code": "unsupported_media_type", "message": f"content-type {file.content_type} not allowed"})

    global SERVICE
    assert SERVICE is not None

    async def read_chunk(n: int) -> bytes:
        return await file.read(n)

    try:
        job, token = await SERVICE.create_job_from_upload(
            filename=file.filename or "upload",
            content_type=file.content_type or "application/octet-stream",
            reader=read_chunk,
            max_upload_mb=MAX_UPLOAD_MB,
        )
    except ValueError as e:
        # payload too large
        raise HTTPException(status_code=413, detail={"code": "payload_too_large", "message": str(e)})

    job_id = job.id
    body = {
        "id": job_id,
        "status": job.data.get("status", "queued"),
        "progress": job.data.get("progress", 0),
        "access_token": token,
        "links": {
            "self": f"/jobs/{job_id}",
            "result": f"/jobs/{job_id}/result",
        },
    }

    headers = {"Location": f"/jobs/{job_id}"}
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=body, headers=headers)


@app.get("/jobs/{job_id}")
async def get_job(job_id: str, authorization: str | None = Header(None)) -> JSONResponse:
    token = _validate_bearer_token(authorization)
    global SERVICE
    assert SERVICE is not None
    try:
        job = SERVICE.load_job(job_id).data
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "job not found"})
    if not SERVICE.verify_token(JobRecord(job), token):
        # 403 per plan
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "invalid token"})
    # Do not expose token hash in response
    redacted = {k: v for k, v in job.items() if k != "access_token_hash"}
    return JSONResponse(content=redacted)


@app.get("/jobs/{job_id}/result", response_class=PlainTextResponse)
async def get_result(job_id: str, authorization: str | None = Header(None)) -> PlainTextResponse:
    token = _validate_bearer_token(authorization)
    global SERVICE
    assert SERVICE is not None
    try:
        job_rec = SERVICE.load_job(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "job not found"})
    if not SERVICE.verify_token(job_rec, token):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "invalid token"})
    output_uri = job_rec.data.get("output_uri")
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

    uvicorn.run("doc_service.webapi:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    run()
