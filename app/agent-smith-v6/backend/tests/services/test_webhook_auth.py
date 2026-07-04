"""Unit tests do gate de auth token-only do webhook Z-API (§3.2 / §7).

O webhook (`backend/app/api/webhook.py`) era o ÚNICO ingress CRÍTICO sem auth: o
tenant era derivado do ``connectedPhone`` controlado pelo atacante. O modelo de
**segredo global** foi ELIMINADO — agora o segmento de path é SEMPRE um token
por-integração (256 bits) e o tenant é resolvido pelo token (lookup O(1) por
``webhook_token_hash`` + ``hmac.compare_digest``), NUNCA pelo corpo.

Estes testes exercem o gate (``_resolve_webhook_token``) + o handler DIRETAMENTE
(sem decorator slowapi, sem TestClient), mirando os critérios de aceite TOKEN:

  - token válido (ATIVO, provider casa) -> ACK + buffer/dispatch acontece;
  - token desconhecido/vazio/revogado (inativo) -> 401 e NADA é bufferizado;
  - provider-mismatch (token de outra rota) -> 401;
  - erro de DB no lookup -> 401 (fail-CLOSED, nunca fail-open);
  - path > 80 chars -> 401 (sem hashear lixo);
  - AST-guard: a comparação do hash usa ``hmac.compare_digest``, nunca ``==``.

Não há mais segredo global nem header de token: qualquer request sem token
VÁLIDO no path é 401 em qualquer forma.

Convenções (espelham tests/services/test_whatsapp_turn_service.py):
  - SEM pytest-asyncio; async via ``asyncio.run(...)``.
  - Plain asserts; colaboradores monkeypatched no módulo webhook.
  - Env semeado por tests/services/conftest.py ANTES de importar app.*.
"""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException

import app.api.webhook as webhook

_PROVIDER = "z-api"
# Token canônico ``wh_{tag}_{base64url(32 bytes)}`` (tag z-api -> zapi).
_TOKEN = "wh_zapi_" + "A" * 43
_INTEGRATION_ID = "int-zapi-1"
_COMPANY_ID = "company-A"


# =========================================================================== #
# Fakes
# =========================================================================== #
class _FakeRequest:
    """Stand-in mínimo para starlette Request: headers + json() awaitable.

    O dispatch de mídia injeta ``request.app.state.supabase_async`` no
    ``process_inbound`` — expomos um sentinel para a asserção. ``client`` existe
    só para o caso de algum colaborador inspecionar o IP (o contador de falhas é
    monkeypatched para no-op nestes testes).
    """

    def __init__(
        self, headers: Optional[Dict[str, str]] = None, body: Optional[dict] = None
    ) -> None:
        self.headers = headers or {}
        self._body = body if body is not None else _text_payload()
        self.app = SimpleNamespace(state=SimpleNamespace(supabase_async=object()))
        self.client = SimpleNamespace(host="203.0.113.7")

    async def json(self) -> dict:
        return self._body


class _FakeBackgroundTasks:
    """Registra add_task sem agendar nada."""

    def __init__(self) -> None:
        self.tasks: List[tuple] = []

    def add_task(self, func: Any, *args: Any, **kwargs: Any) -> None:
        self.tasks.append((func, args, kwargs))


class _FakeBuffer:
    def __init__(self) -> None:
        self.added: List[Dict[str, Any]] = []

    async def add_message(self, **kwargs: Any) -> None:
        self.added.append(kwargs)


def _text_payload() -> Dict[str, Any]:
    return {
        "connectedPhone": "5511999999999",
        "phone": "5544888888888",
        "senderName": "Cliente",
        "text": {"message": "olá"},
    }


def _media_payload() -> Dict[str, Any]:
    return {
        "connectedPhone": "5511999999999",
        "phone": "5544888888888",
        "senderName": "Cliente",
        "image": {"imageUrl": "https://z-api/img.jpg"},
    }


def _integration_row(
    token: str = _TOKEN,
    *,
    provider: str = _PROVIDER,
    is_active: bool = True,
) -> Dict[str, Any]:
    """Linha ``integrations`` resolvida pelo token (hash = sha256(token) hex)."""
    return {
        "id": _INTEGRATION_ID,
        "company_id": _COMPANY_ID,
        "provider": provider,
        "is_active": is_active,
        "webhook_token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "webhook_token_prefix": token[:12],
    }


# =========================================================================== #
# Harness — instala o resolver de token (DB), o buffer e o no-op do contador
# =========================================================================== #
class _FakeIntegrationService:
    """Fake do IntegrationService: lookup por token via ``get_integration_by_
    webhook_token``. Só ATIVAS casam; ``raise_on_lookup`` simula erro de DB que o
    service PROPAGA (fail-closed na borda)."""

    def __init__(
        self, row: Optional[Dict[str, Any]], *, raise_on_lookup: bool = False
    ) -> None:
        self._row = row
        self._raise = raise_on_lookup
        self.lookups: List[str] = []

    def get_integration_by_webhook_token(self, token: str) -> Optional[Dict[str, Any]]:
        self.lookups.append(token)
        if self._raise:
            raise RuntimeError("db down")
        if self._row is None:
            return None
        # Espelha o filtro ``is_active=True`` do service: inativa NÃO casa.
        if not self._row.get("is_active", False):
            return None
        return self._row


def _install_resolver(
    monkeypatch: pytest.MonkeyPatch,
    row: Optional[Dict[str, Any]],
    *,
    raise_on_lookup: bool = False,
) -> _FakeIntegrationService:
    """Monkeypatcha o caminho de resolução do token no módulo webhook.

    ``_resolve_webhook_token`` faz ``get_supabase_client()`` ->
    ``get_integration_service(client)`` -> ``get_integration_by_webhook_token``.
    """
    service = _FakeIntegrationService(row, raise_on_lookup=raise_on_lookup)
    monkeypatch.setattr(
        webhook, "get_supabase_client", lambda: SimpleNamespace(client=object())
    )
    monkeypatch.setattr(webhook, "get_integration_service", lambda client: service)
    return service


def _install_no_op_failure_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutraliza o contador Redis de falhas de auth (testado em rate-limit)."""

    async def _noop(*_a: Any, **_k: Any) -> bool:
        return False

    monkeypatch.setattr(webhook, "record_webhook_auth_failure", _noop)


def _install_buffer(monkeypatch: pytest.MonkeyPatch) -> _FakeBuffer:
    buf = _FakeBuffer()

    async def _get_buffer() -> _FakeBuffer:
        return buf

    monkeypatch.setattr(webhook, "get_message_buffer_service", _get_buffer)
    return buf


# =========================================================================== #
# (a) token desconhecido/vazio/revogado -> 401 e NADA bufferizado/enfileirado
# =========================================================================== #
def test_unknown_token_returns_401_no_enqueue(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resolver(monkeypatch, row=None)  # nenhuma linha casa
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.z_api_webhook_with_token.__wrapped__(_FakeRequest(), bg, _TOKEN)
        )

    assert exc.value.status_code == 401
    # Aceite: sem escrita no buffer, sem background task.
    assert buf.added == []
    assert bg.tasks == []


def test_unknown_token_media_returns_401_no_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_resolver(monkeypatch, row=None)
    _install_no_op_failure_counter(monkeypatch)
    _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.z_api_webhook_with_token.__wrapped__(
                _FakeRequest(body=_media_payload()), bg, _TOKEN
            )
        )

    assert exc.value.status_code == 401
    assert bg.tasks == []


def test_empty_token_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    # Path vazio: rejeitado ANTES de hashear (sem oráculo de validade).
    _install_resolver(monkeypatch, row=_integration_row())
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(webhook.z_api_webhook_with_token.__wrapped__(_FakeRequest(), bg, ""))

    assert exc.value.status_code == 401
    assert buf.added == []


def test_revoked_token_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    # Integração inativa (revogada): o service filtra ``is_active`` => None => 401.
    _install_resolver(monkeypatch, row=_integration_row(is_active=False))
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.z_api_webhook_with_token.__wrapped__(_FakeRequest(), bg, _TOKEN)
        )

    assert exc.value.status_code == 401
    assert buf.added == []


# =========================================================================== #
# (b) provider-mismatch -> 401 (token de outra rota não passa, fail-closed)
# =========================================================================== #
def test_provider_mismatch_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    # A linha casa por hash, mas é de OUTRO provider (uazapi) na rota z-api.
    _install_resolver(monkeypatch, row=_integration_row(provider="uazapi"))
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.z_api_webhook_with_token.__wrapped__(_FakeRequest(), bg, _TOKEN)
        )

    assert exc.value.status_code == 401
    assert buf.added == []
    assert bg.tasks == []


# =========================================================================== #
# (c) erro de DB no lookup -> 401 (fail-CLOSED, nunca fail-open)
# =========================================================================== #
def test_db_error_fails_closed_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resolver(monkeypatch, row=_integration_row(), raise_on_lookup=True)
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.z_api_webhook_with_token.__wrapped__(_FakeRequest(), bg, _TOKEN)
        )

    assert exc.value.status_code == 401
    assert buf.added == []
    assert bg.tasks == []


# =========================================================================== #
# (d) path > 80 chars -> 401 (rejeitado ANTES de hashear)
# =========================================================================== #
def test_oversized_token_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resolver(monkeypatch, row=_integration_row())
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    oversized = "wh_zapi_" + "B" * 200  # > 80 chars
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.z_api_webhook_with_token.__wrapped__(_FakeRequest(), bg, oversized)
        )

    assert exc.value.status_code == 401
    assert buf.added == []


# =========================================================================== #
# (e) AST-guard: a comparação do hash do token usa hmac.compare_digest, não ==
# =========================================================================== #
def test_auth_uses_compare_digest_not_equality() -> None:
    """Guarda contra regressão para ``==`` na comparação do hash do token.

    O gate ÚNICO token-only é ``_resolve_webhook_token``: re-aponta o AST-guard
    (antes no verificador de segredo global, hoje removido) para a FONTE onde a
    regressão de ``compare_digest`` -> ``==`` poderia acontecer.
    """
    import ast
    import inspect

    src = inspect.getsource(webhook._resolve_webhook_token)
    assert "hmac.compare_digest" in src
    # Inspeciona só o código executável (a docstring legitimamente menciona "==").
    tree = ast.parse(src.strip())
    eq_compares = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops)
    ]
    # O gate compara provider/hash com identidade/igualdade lógica controlada, mas
    # a comparação SENSÍVEL (hash do token) DEVE ser timing-safe. Garantimos que o
    # hash do token nunca passe por ``==``: a única comparação constante-time é a
    # do hash, e ela usa ``compare_digest``.
    assert "row_hash" not in {
        getattr(node.left, "id", None) for node in eq_compares
    }, "o hash do token não pode ser comparado com ==/!="


# =========================================================================== #
# (f) token válido (header NÃO existe mais) -> ACK + enqueue acontece
# =========================================================================== #
def test_valid_token_buffers_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resolver(monkeypatch, row=_integration_row())
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    result = asyncio.run(
        webhook.z_api_webhook_with_token.__wrapped__(_FakeRequest(), bg, _TOKEN)
    )

    assert result == {"status": "buffered", "phone": "5544888888888"}
    assert len(buf.added) == 1
    # Tenant REAL da integração resolvida pelo token (≠ 'pending'); integration_id
    # presente; user_id AINDA 'pending' (nasce só após o guard em process_inbound).
    added = buf.added[0]
    assert added["company_id"] == _COMPANY_ID
    assert added["integration_id"] == _INTEGRATION_ID
    assert added["user_id"] == "pending"


def test_valid_token_dispatches_media(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resolver(monkeypatch, row=_integration_row())
    _install_no_op_failure_counter(monkeypatch)
    _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    req = _FakeRequest(body=_media_payload())
    result = asyncio.run(webhook.z_api_webhook_with_token.__wrapped__(req, bg, _TOKEN))

    assert result == {"status": "received", "type": "media"}
    assert len(bg.tasks) == 1
    # O dispatch delega DIRETO ao service (process_inbound), com o
    # AsyncSupabaseClient real do lifespan injetado por keyword.
    assert bg.tasks[0][0] is webhook.process_inbound
    assert bg.tasks[0][2]["async_supabase_client"] is req.app.state.supabase_async


# =========================================================================== #
# (f.2) bound de vazão por tenant no SUCESSO (§5 "Rate limiting" (b)): um token
#       VÁLIDO que estoura WEBHOOK_INTEGRATION_LIMIT na janela -> 429, sem
#       bufferizar. Garante que ``record_webhook_integration_hit`` está WIRED no
#       resolver (não é dead code).
# =========================================================================== #
def test_valid_token_over_integration_limit_returns_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_resolver(monkeypatch, row=_integration_row())
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    seen_ids: List[str] = []

    async def _over_limit(integration_id: str) -> bool:
        # Contador por integração: simula estouro do teto da janela (True).
        seen_ids.append(integration_id)
        return True

    monkeypatch.setattr(webhook, "record_webhook_integration_hit", _over_limit)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            webhook.z_api_webhook_with_token.__wrapped__(_FakeRequest(), bg, _TOKEN)
        )

    assert exc.value.status_code == 429
    # O counter foi chamado com o integration_id da linha do TOKEN.
    assert seen_ids == [_INTEGRATION_ID]
    # 429 ANTES do handler: nada bufferizado/enfileirado.
    assert buf.added == []
    assert bg.tasks == []


def test_valid_token_under_integration_limit_buffers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Dentro do teto (counter -> False): segue o fluxo normal e bufferiza.
    _install_resolver(monkeypatch, row=_integration_row())
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    async def _under_limit(integration_id: str) -> bool:
        return False

    monkeypatch.setattr(webhook, "record_webhook_integration_hit", _under_limit)

    result = asyncio.run(
        webhook.z_api_webhook_with_token.__wrapped__(_FakeRequest(), bg, _TOKEN)
    )

    assert result == {"status": "buffered", "phone": "5544888888888"}
    assert len(buf.added) == 1


# =========================================================================== #
# (g) anti-injeção do carrier: __edge_integration_id forjado no corpo é ignorado;
#     o tenant resolvido vem SÓ do token (strip-then-set).
# =========================================================================== #
def test_forged_carrier_in_body_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resolver(monkeypatch, row=_integration_row())
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    forged = _text_payload()
    forged["__edge_integration_id"] = "attacker-tenant-999"
    result = asyncio.run(
        webhook.z_api_webhook_with_token.__wrapped__(
            _FakeRequest(body=forged), bg, _TOKEN
        )
    )

    assert result == {"status": "buffered", "phone": "5544888888888"}
    assert len(buf.added) == 1
    # O carrier no payload canônico carrega o id do TOKEN, não o forjado no corpo.
    assert buf.added[0]["payload"]["__edge_integration_id"] == _INTEGRATION_ID
    assert buf.added[0]["company_id"] == _COMPANY_ID


# =========================================================================== #
# (h) forja-bloqueada: connectedPhone forjado NÃO muda o tenant (vem do token).
# =========================================================================== #
def test_forged_connected_phone_resolves_token_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_resolver(monkeypatch, row=_integration_row())
    _install_no_op_failure_counter(monkeypatch)
    buf = _install_buffer(monkeypatch)
    bg = _FakeBackgroundTasks()

    forged = _text_payload()
    forged["connectedPhone"] = "5599000000000"  # número de outro tenant
    asyncio.run(
        webhook.z_api_webhook_with_token.__wrapped__(
            _FakeRequest(body=forged), bg, _TOKEN
        )
    )

    assert len(buf.added) == 1
    # company_id e integration_id vêm da linha do TOKEN, não do connectedPhone.
    assert buf.added[0]["company_id"] == _COMPANY_ID
    assert buf.added[0]["integration_id"] == _INTEGRATION_ID
