"""
Testes de auth + SSRF dos endpoints UCP (F02).

Cobrem:
- validate_external_url rejeita loopback/privado/link-local/metadata/http
  (guarda reutilizada por ucp_discovery).
- discover() NÃO emite GET externo quando a store_url resolve para alvo
  bloqueado e retorna success=False ("URL bloqueada pela política de segurança").
- Redirect (3xx) para alvo interno é bloqueado (follow_redirects=False +
  revalidação manual de cada Location).
- Os endpoints /api/ucp/discover e /connect exigem
  Depends(require_trusted_tenant_claims): sem X-Admin-API-Key/JWT → 401/403.

Sem pytest-asyncio: usamos asyncio.run(), padrão do projeto.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core.security.url_validator import (
    ExternalUrlValidationError,
    validate_external_url,
)
from app.services.ucp_discovery import UCPDiscoveryService


# ---------------------------------------------------------------------------
# (c) validate_external_url bloqueia os alvos sensíveis
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/.well-known/ucp",   # http (apenas https)
        "https://127.0.0.1/.well-known/ucp",     # loopback
        "https://localhost/.well-known/ucp",     # localhost
        "https://169.254.169.254/latest/meta",   # metadata link-local
        "https://10.0.0.5/.well-known/ucp",       # privado
        "https://[::1]/.well-known/ucp",          # ipv6 loopback
    ],
)
def test_validate_external_url_blocks_ssrf_targets(url):
    with pytest.raises(ExternalUrlValidationError):
        validate_external_url(url)


# ---------------------------------------------------------------------------
# (a) discover() não faz GET externo para alvo bloqueado
# ---------------------------------------------------------------------------

def test_discover_blocked_url_emits_no_outbound_get():
    service = UCPDiscoveryService()
    # Se algum GET externo for emitido, o teste falha (loja resolve p/ loopback).
    service._http_client = SimpleNamespace(get=AsyncMock())

    result = asyncio.run(service.discover("127.0.0.1"))

    assert result.success is False
    assert result.error == "URL bloqueada pela política de segurança"
    service._http_client.get.assert_not_called()


def test_discover_private_host_blocked():
    service = UCPDiscoveryService()
    service._http_client = SimpleNamespace(get=AsyncMock())

    result = asyncio.run(service.discover("10.10.10.10"))

    assert result.success is False
    assert result.error == "URL bloqueada pela política de segurança"
    service._http_client.get.assert_not_called()


# ---------------------------------------------------------------------------
# (d) redirect para alvo interno é bloqueado
# ---------------------------------------------------------------------------

def test_discover_redirect_to_internal_is_blocked():
    """
    Primeiro hop (host público) responde 302 -> http(s)://10.0.0.9/...; a
    revalidação manual deve bloquear o segundo hop e retornar success=False,
    sem nunca buscar o manifest interno.
    """
    service = UCPDiscoveryService()

    redirect_resp = SimpleNamespace(
        is_redirect=True,
        status_code=302,
        headers={"Location": "https://10.0.0.9/.well-known/ucp"},
        url=SimpleNamespace(join=lambda loc: "https://10.0.0.9/.well-known/ucp"),
    )
    get_mock = AsyncMock(return_value=redirect_resp)
    service._http_client = SimpleNamespace(get=get_mock)

    # store_url público válido: validate_external_url do 1º hop passa (mockado),
    # mas o Location interno é revalidado e bloqueado.
    with patch(
        "app.services.ucp_discovery.validate_external_url"
    ) as validate_mock:
        def _validate(url):
            if "10.0.0.9" in url:
                raise ExternalUrlValidationError("blocked internal redirect")
            return SimpleNamespace(normalized_url=url, hostname="store.example.com")

        validate_mock.side_effect = _validate
        result = asyncio.run(service.discover("https://store.example.com"))

    assert result.success is False
    assert result.error == "URL bloqueada pela política de segurança"
    # Apenas o 1º GET (público) foi emitido; o host interno nunca foi buscado.
    assert get_mock.await_count == 1


# ---------------------------------------------------------------------------
# (auth) endpoints exigem require_trusted_tenant_claims
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _admin_api_key_env(monkeypatch):
    # require_master_admin retorna 500 ("not configured") quando ADMIN_API_KEY
    # está ausente do ambiente. Com a key setada e SEM o header X-Admin-API-Key,
    # a rejeição é o 401 que estes testes verificam.
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")


def _build_ucp_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.ucp import router
    from app.core.database import get_async_db

    app = FastAPI()
    app.include_router(router, prefix="/api")
    # get_async_db lê request.app.state.supabase_async, inexistente neste app de
    # teste (sem lifespan). Sem o override, a dependency estoura AttributeError ->
    # 500 ANTES da checagem de auth. Com o fake, a AUTH REAL
    # (require_master_admin / require_internal_user_claims) roda e rejeita a
    # request sem credenciais com 401/403 — que é o que queremos provar.
    app.dependency_overrides[get_async_db] = lambda: object()
    return TestClient(app, raise_server_exceptions=False)


def test_discover_requires_auth():
    client = _build_ucp_client()
    resp = client.post("/api/ucp/discover", json={"store_url": "https://x.com"})
    assert resp.status_code in (401, 403)


def test_connect_requires_auth():
    client = _build_ucp_client()
    resp = client.post(
        "/api/ucp/connect",
        json={
            "agent_id": "00000000-0000-0000-0000-000000000000",
            "company_id": "11111111-1111-1111-1111-111111111111",
            "store_url": "https://x.com",
        },
    )
    assert resp.status_code in (401, 403)


def test_list_connections_requires_auth():
    client = _build_ucp_client()
    resp = client.get("/api/ucp/connections/00000000-0000-0000-0000-000000000000")
    assert resp.status_code in (401, 403)
