"""F27 (G7-R6) — multipart upload smoke tests for the two reachable upload
endpoints after bumping `python-multipart` to >=0.0.18 (CVE-2024-53981).

These prove that a VALID `multipart/form-data` POST is still parsed correctly by
the bumped library on both `documents.py` and `sanitization.py`: the request
goes through the real Starlette/`python-multipart` form parser (no parsing is
faked), reaches the handler, and returns 2xx — and the handler actually receives
the uploaded file bytes + filename + the `Form(...)` fields.

Everything BELOW the parser is faked so no external service is touched:
  - `require_trusted_tenant_claims` is overridden (no API key / JWT needed);
  - the document/sanitization service factories are monkeypatched to in-memory
    fakes that just record what the parser handed them;
  - the agent-ownership check and the background task are stubbed out.

Conventions (mirror tests/services/test_ucp_auth_ssrf.py):
  - A fresh FastAPI app mounts ONLY the router under test, then TestClient.
  - Env vars are seeded by tests/services/conftest.py BEFORE importing app.*.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.documents as documents_mod
import app.api.sanitization as sanitization_mod
from app.core.auth import InternalJwtClaims, require_trusted_tenant_claims

_COMPANY_ID = "11111111-1111-1111-1111-111111111111"
_AGENT_ID = "22222222-2222-2222-2222-222222222222"
_PDF_BYTES = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\nsmoke-test-body\n%%EOF\n"


def _fake_claims() -> InternalJwtClaims:
    """Claims whose company_id matches the multipart `company_id` field, so
    `ensure_internal_company_access` passes inside the handler."""
    now = int(time.time())
    return InternalJwtClaims(
        company_id=_COMPANY_ID,
        role="admin",
        actor_type="company_admin",
        iat=now,
        exp=now + 3600,
        user_id="user-1",
    )


# --------------------------------------------------------------------------- #
# documents.py /documents/upload
# --------------------------------------------------------------------------- #
class _FakeDocumentService:
    def __init__(self) -> None:
        self.received: Dict[str, Any] = {}

    def upload_document(
        self,
        *,
        file_data: Any,
        filename: str,
        company_id: str,
        file_size: int,
        content_type: str,
        agent_id: str,
    ) -> str:
        # file_data is a BytesIO built from the parsed multipart body.
        self.received = {
            "body": file_data.read(),
            "filename": filename,
            "company_id": company_id,
            "file_size": file_size,
            "content_type": content_type,
            "agent_id": agent_id,
        }
        return "doc-smoke-1"


def test_documents_upload_valid_multipart_is_parsed(monkeypatch):
    fake_service = _FakeDocumentService()
    monkeypatch.setattr(documents_mod, "get_document_service", lambda: fake_service)

    # Bypass the heavy I/O the handler would otherwise do around the parse.
    async def _ok_agent(_agent_id: str, _company_id: str) -> None:
        return None

    monkeypatch.setattr(documents_mod, "_ensure_agent_belongs_to_company", _ok_agent)
    monkeypatch.setattr(documents_mod, "process_document_task", lambda *a, **k: None)

    app = FastAPI()
    app.include_router(documents_mod.router, tags=["Documents"])
    app.dependency_overrides[require_trusted_tenant_claims] = _fake_claims
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/documents/upload",
        files={"file": ("smoke.pdf", _PDF_BYTES, "application/pdf")},
        data={
            "company_id": _COMPANY_ID,
            "agent_id": _AGENT_ID,
            "strategy": "semantic",
            "ingestion_mode": "semantic",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document_id"] == "doc-smoke-1"
    assert body["agent_id"] == _AGENT_ID
    # The multipart parser handed the handler the exact bytes + metadata.
    assert fake_service.received["body"] == _PDF_BYTES
    assert fake_service.received["filename"] == "smoke.pdf"
    assert fake_service.received["company_id"] == _COMPANY_ID


# --------------------------------------------------------------------------- #
# sanitization.py /api/sanitization/upload
# --------------------------------------------------------------------------- #
class _FakeSanitizationService:
    def __init__(self) -> None:
        self.received: Dict[str, Any] = {}
        self.processed: List[str] = []

    def upload(
        self,
        *,
        file_data: bytes,
        filename: str,
        company_id: str,
        file_size: int,
        content_type: str,
        extract_images: bool = False,
    ) -> str:
        self.received = {
            "body": file_data,
            "filename": filename,
            "company_id": company_id,
            "file_size": file_size,
            "content_type": content_type,
            "extract_images": extract_images,
        }
        return "job-smoke-1"

    def process(self, job_id: str) -> None:
        self.processed.append(job_id)


def test_sanitization_upload_valid_multipart_is_parsed(monkeypatch):
    fake_service = _FakeSanitizationService()
    monkeypatch.setattr(
        sanitization_mod, "get_sanitization_service", lambda: fake_service
    )
    # Force the BackgroundTask path (not Celery) so no broker is needed; the task
    # only calls the faked service.process.
    monkeypatch.setattr(sanitization_mod.settings, "USE_CELERY", False, raising=False)

    app = FastAPI()
    app.include_router(sanitization_mod.router, prefix="/api/sanitization", tags=["Sanitization"])
    app.dependency_overrides[require_trusted_tenant_claims] = _fake_claims
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/api/sanitization/upload",
        files={"file": ("smoke.pdf", _PDF_BYTES, "application/pdf")},
        data={"company_id": _COMPANY_ID, "extract_images": "false"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == "job-smoke-1"
    assert body["status"] == "pending"
    # The multipart parser handed the handler the exact bytes + metadata.
    assert fake_service.received["body"] == _PDF_BYTES
    assert fake_service.received["filename"] == "smoke.pdf"
    assert fake_service.received["company_id"] == _COMPANY_ID
