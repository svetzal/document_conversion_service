import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .interfaces import ConverterGateway, StorageGateway, SecurityGateway


class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass
class JobRecord:
    data: dict[str, object]

    @property
    def id(self) -> str:
        return str(self.data["id"])  # type: ignore[index]


class ConversionService:
    """Core domain service orchestrating conversion jobs.

    This service is framework-agnostic. It exposes async methods for
    creating jobs and running worker loops, while using gateways to
    interact with storage, security, and document conversion.
    """

    def __init__(
        self,
        storage: StorageGateway,
        security: SecurityGateway,
        converter: ConverterGateway,
        *,
        workers: int = 4,
    ) -> None:
        self._storage = storage
        self._security = security
        self._converter = converter
        self._workers = workers
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []

    @property
    def queue(self) -> asyncio.Queue[str]:
        return self._queue

    async def start(self) -> None:
        for i in range(self._workers):
            task = asyncio.create_task(self._worker_loop(f"worker-{i+1}"))
            self._tasks.append(task)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()

    # API used by HTTP controller to create a job from upload stream
    async def create_job_from_upload(
        self,
        filename: str,
        content_type: str,
        reader: Callable[[int], "asyncio.Future[bytes] | asyncio.Future[bytearray] | asyncio.Future[memoryview]"],
        *,
        max_upload_mb: int,
    ) -> tuple[JobRecord, str]:
        """Persist upload to storage, create metadata, enqueue job and return job + one-time token."""
        import uuid
        job_id = str(uuid.uuid4())
        token = self._security.new_token()
        token_hash = self._security.hash_token(token)

        data_dir = Path(os.getenv("DATA_DIR", "./data")).resolve()
        job_dir = data_dir / "jobs" / job_id
        input_dir = job_dir / "input"
        output_dir = job_dir / "output"
        artifacts_dir = job_dir / "artifacts"
        for d in (input_dir, output_dir, artifacts_dir):
            d.mkdir(parents=True, exist_ok=True)

        # Choose safe filename
        original_name = filename or "upload"
        ext = ""
        if "." in original_name:
            ext = "." + original_name.rsplit(".", 1)[-1]
        input_path = input_dir / f"original{ext}"

        # Stream upload and compute checksum
        import hashlib
        sha256 = hashlib.sha256()
        size_bytes = 0
        CHUNK = 1024 * 1024
        max_bytes = max_upload_mb * 1024 * 1024
        with input_path.open("wb") as f_out:
            while True:
                chunk = await reader(CHUNK)
                if not chunk:
                    break
                b = bytes(chunk)
                size_bytes += len(b)
                if size_bytes > max_bytes:
                    f_out.close()
                    try:
                        input_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    raise ValueError(f"upload exceeds {max_upload_mb} MB")
                f_out.write(b)
                sha256.update(b)

        checksum_hex = sha256.hexdigest()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        job_meta: dict[str, object] = {
            "id": job_id,
            "filename": original_name,
            "content_type": content_type or "application/octet-stream",
            "size_bytes": size_bytes,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "completed_at": None,
            "failed_at": None,
            "status": JobStatus.QUEUED,
            "progress": 0,
            "error": None,
            "input_uri": str(input_path),
            "output_uri": None,
            "artifacts": [],
            "checksum": checksum_hex,
            "access_token_hash": token_hash,
        }
        # Persist
        with (job_dir / "job.json").open("w", encoding="utf-8") as f:
            json.dump(job_meta, f, ensure_ascii=False, indent=2)

        # Enqueue
        await self._queue.put(job_id)

        return JobRecord(job_meta), token

    def load_job(self, job_id: str) -> JobRecord:
        data = self._storage.load_job(job_id)
        return JobRecord(data)

    def verify_token(self, job: JobRecord, token: str) -> bool:
        phc = str(job.data.get("access_token_hash", ""))
        return self._security.verify(phc, token)

    async def _worker_loop(self, name: str) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                job = self._storage.load_job(job_id)
                now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                job["status"] = JobStatus.RUNNING
                job["started_at"] = now
                job["updated_at"] = now
                self._storage.save_job(job)

                input_uri = str(job["input_uri"])  # type: ignore[index]
                output_dir = Path(self._storage.job_dir(job_id)) / "output"
                output_dir.mkdir(parents=True, exist_ok=True)
                output_path = output_dir / "result.md"

                md = await asyncio.to_thread(self._converter.convert_to_markdown, input_uri)

                def write_output() -> None:
                    with output_path.open("w", encoding="utf-8") as f:
                        f.write(md)

                await asyncio.to_thread(write_output)

                now2 = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                job["progress"] = 100
                job["output_uri"] = str(output_path)
                job["status"] = JobStatus.SUCCEEDED
                job["completed_at"] = now2
                job["updated_at"] = now2
                self._storage.save_job(job)
            except Exception as e:
                try:
                    j = self._storage.load_job(job_id)
                    j["status"] = JobStatus.FAILED
                    j["error"] = str(e)
                    j["failed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    j["updated_at"] = j["failed_at"]
                    self._storage.save_job(j)
                except Exception:
                    pass
            finally:
                self._queue.task_done()
