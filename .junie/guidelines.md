# Project Guidelines — Document Conversion Service

The intent of this project is to provide a RESTful web API for converting documents in complex and proprietary formats into plain Markdown for effective consumption by Large Language Models (LLMs).

## Tech Stack and Versions
- Python: >= 3.13 (enforced in pyproject.toml)
- API framework: FastAPI
- ASGI server: Uvicorn
- Build backend: Hatchling (PEP 517/621), src/ layout
- Package name: `document-conversion-service`; src module: `doc_service`
- Future conversion engine: `docling` (planned)

## Python 3.13+ Conventions
- Use builtin generics:
  - Prefer `dict[str, T]`, `list[T]`, `set[T]`, etc. over `typing.Dict`, `typing.List`, `typing.Set`.
- Use modern union syntax:
  - Prefer `str | None` over `Optional[str]`.
- Do not add `from __future__ import annotations`:
  - Python 3.13 defaults to deferred/lazy annotation evaluation; the future import is unnecessary.
- Typing guidance:
  - Avoid `Any` unless required; prefer precise types.
  - Use TypedDict/Dataclasses/Pydantic models for structured payloads where appropriate.
  - Keep imports minimal to avoid import-time side effects.

## Project Layout
- Source code lives under `src/` using a package layout:
  - `src/doc_service/__init__.py`
  - `src/doc_service/main.py` (FastAPI app)
- Build config and metadata are in `pyproject.toml`.

## Local Development
Default (minimal) workflow using venv + pip/uv:
1) Create a virtual environment:
   - `python -m venv .venv` (or `uv venv`)
2) Activate it:
   - macOS/Linux: `source .venv/bin/activate`
   - Windows: `.venv\Scripts\activate`
3) Install in editable mode:
   - `pip install -e .` (or `uv pip install -e .`)

Maintain PEP 621 compliance in pyproject.toml, Do not use poetry specific configuration when there is a PEP 621 equivalent.

- Enable in-project venv: `poetry config virtualenvs.in-project true --local`
- Install deps and create venv: `poetry install`
- Run commands inside venv: `poetry run <cmd>` or `poetry shell`

We currently use Hatchling purely as a build backend. Dependency management is left to poetry.

## Running the Service
- Via console script (after install): `doc-service`
- Or with Uvicorn directly: `uvicorn doc_service.main:app --reload`
- Default host/port: `0.0.0.0:8080` (override with `HOST`/`PORT` env vars)

Endpoints (initial):
- `GET /hello` -> `{ "message": "hello world" }`
- `GET /health` -> `{ "status": "ok" }`

## Future Work
- Integrate `docling` for document parsing/conversion.
- Define API endpoints for upload, conversion options, and Markdown output.
- Add request/response schemas (Pydantic) and validation.
- Establish testing, linting, and CI guidelines.
