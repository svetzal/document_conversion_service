import os

from fastapi import FastAPI

app = FastAPI(
    title="Document Conversion Service",
    version=os.getenv("DOC_SERVICE_VERSION", "0.1.0"),
    description=(
        "RESTful API for converting complex and proprietary documents into "
        "plan Markdown for LLM consumption."
    ),
)


@app.get("/hello")
def hello_world() -> dict[str, str]:
    """Simple hello-world endpoint.

    Returns a JSON payload: {"message": "hello world"}
    """
    return {"message": "hello world"}


@app.get("/health")
def health() -> dict[str, str]:
    """Basic health check endpoint."""
    return {"status": "ok"}


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
