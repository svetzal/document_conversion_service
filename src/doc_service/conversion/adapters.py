import base64
import os
import secrets
from pathlib import Path

from .interfaces import ConverterGateway, StorageGateway, SecurityGateway


class LocalStorage(StorageGateway):
    def __init__(self, data_dir: str) -> None:
        self._base = Path(data_dir).resolve()

    def job_dir(self, job_id: str) -> str:
        return str(self._base / "jobs" / job_id)

    def save_job(self, job: dict[str, object]) -> None:
        job_id = str(job["id"])  # type: ignore[index]
        p = Path(self.job_dir(job_id)) / "job.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        import json
        with p.open("w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False, indent=2)

    def load_job(self, job_id: str) -> dict[str, object]:
        p = Path(self.job_dir(job_id)) / "job.json"
        if not p.exists():
            raise FileNotFoundError("job not found")
        import json
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)


class Argon2Security(SecurityGateway):
    def new_token(self) -> str:
        raw = secrets.token_bytes(32)
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    def hash_token(self, token: str) -> str:
        """Hash the token as unpadded base64url-encoded SHA-256 digest of raw token bytes.

        This replaces the previous Argon2 PHC storage to align with the plan.
        Backward compatibility is maintained in verify().
        """
        import hashlib
        raw = self._b64url_to_bytes(token)
        digest = hashlib.sha256(raw).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def verify(self, phc_hash: str, token: str) -> bool:
        """Verify a token against either legacy Argon2 PHC or new SHA-256 base64url hash.

        - Legacy: if phc_hash starts with "$argon2", use argon2.low_level.verify_secret.
        - New: compute sha256 over raw token bytes and compare base64url (no padding).
        """
        try:
            if phc_hash.startswith("$argon2"):
                from argon2.low_level import verify_secret
                raw = self._b64url_to_bytes(token)
                return verify_secret(phc_hash.encode("utf-8"), raw)
            import hashlib, hmac
            raw = self._b64url_to_bytes(token)
            digest = hashlib.sha256(raw).digest()
            calc = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
            return hmac.compare_digest(calc, phc_hash)
        except Exception:
            return False

    @staticmethod
    def _b64url_to_bytes(token: str) -> bytes:
        pad = "=" * (-len(token) % 4)
        return base64.urlsafe_b64decode(token + pad)


class DoclingConverter(ConverterGateway):
    def convert_to_markdown(self, input_uri: str) -> str:
        # Try DocumentConverter first
        try:
            from docling.document_converter import DocumentConverter  # type: ignore
            converter = DocumentConverter()
            result = converter.convert(input_uri)
            # generic extraction across variants
            try:
                doc = result.document  # type: ignore[attr-defined]
            except Exception:
                to_doc = getattr(result, "to_doc", None)
                doc = to_doc() if callable(to_doc) else result
            # markdown methods variants
            for m in ("export_to_markdown", "to_markdown", "as_markdown"):
                fn = getattr(doc, m, None)
                if callable(fn):
                    return fn()
            raise RuntimeError("Doc object lacks markdown export method")
        except Exception:
            pass

        # Fallback to StandardPdfPipeline tolerant init
        from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline
        pipe = None
        try:
            from docling.pipeline.standard_pdf_pipeline import StandardPdfPipelineOptions  # type: ignore
            opts = StandardPdfPipelineOptions()
            pipe = StandardPdfPipeline(pipeline_options=opts)
        except Exception:
            pass
        if pipe is None:
            get_opts = getattr(StandardPdfPipeline, "get_default_options", None)
            if callable(get_opts):
                try:
                    opts = get_opts()
                    pipe = StandardPdfPipeline(pipeline_options=opts)
                except Exception:
                    pass
        if pipe is None:
            try:
                from docling.pipeline.standard_pdf_pipeline_options import StandardPdfPipelineOptions as StdPdfOpts  # type: ignore
                opts = StdPdfOpts()
                pipe = StandardPdfPipeline(pipeline_options=opts)
            except Exception:
                pass
        if pipe is None:
            try:
                pipe = StandardPdfPipeline()
            except Exception as e:
                raise RuntimeError("Docling pipeline initialization failed across variants") from e

        # Run across possible method names
        for m in ("run", "run_pdf", "process", "__call__"):
            fn = getattr(pipe, m, None)
            if callable(fn):
                try:
                    result = fn(input_uri)
                    break
                except Exception as e:
                    last_err = e
        else:
            try:
                from docling.pipeline.standard_pdf_pipeline import run_pipeline  # type: ignore
                result = run_pipeline(pipe, input_uri)
            except Exception as e:
                raise RuntimeError("Docling pipeline lacks usable run method") from e

        # Extract document and to markdown
        from docling_core.types.doc import DoclingDocument
        doc = None
        if hasattr(result, "document"):
            doc = result.document  # type: ignore[attr-defined]
        elif hasattr(result, "to_doc") and callable(getattr(result, "to_doc")):
            doc = result.to_doc()
        elif isinstance(result, DoclingDocument):
            doc = result
        else:
            raise RuntimeError("Unexpected result type from Docling pipeline")

        for m in ("export_to_markdown", "to_markdown", "as_markdown"):
            fn = getattr(doc, m, None)
            if callable(fn):
                return fn()
        raise RuntimeError("Doc object does not provide a markdown export method")
