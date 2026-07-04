"""Unit tests do whatsapp_turn_service (SPEC §5.1, D4/D5 — Fase 4a).

Exercita ``process_inbound`` — a ÚNICA função pública do pipeline WhatsApp
movido de ``webhook.py`` — com fakes:

  - client async fake (forma do ``AsyncSupabaseClient`` real) injetado no store;
  - provider fake via ``resolve_provider`` + fachada fake via ``WhatsAppService``
    (send_message 2-arg, ponte ``asyncio.to_thread`` inclusa);
  - runner/billing stub via monkeypatch do factory (``build_whatsapp_turn_runner``).

Casos felizes: texto; texto coalescido (combined_message); áudio (placeholder
pré-turno, transcrição SÓ após TurnProceed, sem transcrição em rejected/
handoff); imagem com e sem caption; integração ausente (abort silencioso);
TurnRejected -> copy canônica; TurnHandoff -> silêncio.

Casos de erro do contrato §5.1:
  (a) never-raise — exceção arbitrária no pipeline NÃO propaga ao caller;
  (b) payload sem conteúdo válido -> return sem envio e sem turno;
  (c) get_or_create lançando exceção -> turno SEGUE para o runner;
  (d) Whisper falhando pós-TurnProceed -> sender recebe exatamente
      'Erro ao processar áudio.' e o corpo do turno NÃO roda;
  (e) sender lançando/retornando False -> nenhuma re-invocação do corpo.

Critério da Diagnosis: este arquivo NÃO importa ``app.api.webhook``.

Convenções (espelham test_webhook_seam.py / test_renderers.py):
  - sem pytest-asyncio; async dirigido com ``asyncio.run(...)``;
  - asserts simples; colaboradores monkeypatchados NO MÓDULO do service;
  - env vars semeadas por tests/services/conftest.py ANTES de importar app.*.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

import app.services.whatsapp_turn_service as wts
from app.services.chat_turn_orchestrator import TurnRequest, TurnResult
from app.services.turn_ports.renderers import COPY_INDISPONIVEL

# Id da integração carimbado pela borda token-only no canonical
# (``__edge_integration_id``). ``process_inbound`` resolve o tenant por ESTE id
# (``get_integration_by_id``), nunca pelo ``connectedPhone`` forjável do corpo.
TEST_INTEGRATION_ID = "int-test-1"
from app.services.turn_ports.turn_runner import (
    TurnHandoff,
    TurnProceed,
    TurnRejected,
)


# =========================================================================== #
# Fakes
# =========================================================================== #
class FakeAsyncSupabaseClient:
    """Forma do ``AsyncSupabaseClient`` real: ``.client`` é @property (raw)."""

    def __init__(self) -> None:
        self._raw = object()  # AsyncClient cru (opaco para estes testes)

    @property
    def client(self) -> Any:
        return self._raw


class _BlocklistQuery:
    """Query builder mínimo p/ a tabela internal_whatsapp_blocklist do guard."""

    def __init__(self, store: "BlocklistAsyncClient", table: str) -> None:
        self._store = store
        self._table = table
        self._op = "select"
        self._payload: Any = None
        self._filters: Dict[str, Any] = {}

    def select(self, *_a: Any, **_k: Any) -> "_BlocklistQuery":
        self._op = "select"
        return self

    def update(self, payload: Any, *_a: Any, **_k: Any) -> "_BlocklistQuery":
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, col: str, val: Any) -> "_BlocklistQuery":
        self._filters[col] = val
        return self

    def limit(self, *_a: Any, **_k: Any) -> "_BlocklistQuery":
        return self

    async def execute(self) -> Any:
        rows = self._store.tables.get(self._table, [])

        def _match(r: Dict[str, Any]) -> bool:
            return all(r.get(k) == v for k, v in self._filters.items())

        if self._op == "select":
            out = [dict(r) for r in rows if _match(r)]
            return type("R", (), {"data": out})()
        if self._op == "update":
            updated = []
            for r in rows:
                if _match(r):
                    r.update(self._payload)
                    updated.append(dict(r))
            return type("R", (), {"data": updated})()
        return type("R", (), {"data": []})()


class _BlocklistRawClient:
    def __init__(self, store: "BlocklistAsyncClient") -> None:
        self._store = store

    def table(self, name: str) -> _BlocklistQuery:
        return _BlocklistQuery(self._store, name)


class BlocklistAsyncClient:
    """AsyncSupabaseClient fake cujo ``.client`` resolve a blocklist interna.

    Diferente de ``FakeAsyncSupabaseClient`` (``.client`` opaco), este expõe um
    ``table('internal_whatsapp_blocklist')`` real para exercitar o SELECT do guard
    SEM cair no except silencioso de is_blocked (internal_whatsapp_guard.py:74-76).
    """

    def __init__(self, blocklist_rows: Optional[List[Dict[str, Any]]] = None) -> None:
        self.tables: Dict[str, List[Dict[str, Any]]] = {
            "internal_whatsapp_blocklist": [dict(r) for r in (blocklist_rows or [])]
        }
        self._raw = _BlocklistRawClient(self)

    @property
    def client(self) -> Any:
        return self._raw


class FakePrepared:
    """Corpo do turno devolvido dentro de um ``TurnProceed`` fake."""

    def __init__(self, response: str = "resposta-ia") -> None:
        self.response = response
        self.run_calls = 0
        self.last_req: Optional[TurnRequest] = None

    async def run_aggregate(self, req: TurnRequest) -> TurnResult:
        self.run_calls += 1
        self.last_req = req
        return TurnResult(response=self.response, tokens_total=1)


class FakeRunner:
    """Grava o pré-turno e devolve um TransportEvent pré-definido."""

    def __init__(self, event: Any) -> None:
        self._event = event
        self.last_req: Optional[TurnRequest] = None
        self.calls = 0

    async def resolve_pre_turn(
        self, req: TurnRequest, *, persist_inbound_on_rejected: Optional[bool] = None
    ) -> Any:
        self.calls += 1
        self.last_req = req
        return self._event


class FakeAudioService:
    """Stand-in do AudioService — transcrição async, sem Whisper/rede."""

    instances: List["FakeAudioService"] = []
    raise_on_transcribe = False

    def __init__(self, _api_key: str) -> None:
        self.calls = 0
        FakeAudioService.instances.append(self)

    async def transcribe_audio_from_url(
        self, url: str, *, company_id: str, agent_id: Optional[str]
    ) -> str:
        self.calls += 1
        if FakeAudioService.raise_on_transcribe:
            raise RuntimeError("whisper down")
        return "transcrição-real"


class FakeStore:
    """ConversationStore fake — grava o client injetado e os get_or_create."""

    instances: List["FakeStore"] = []
    get_or_create_raises = False

    def __init__(self, async_supabase_client: Any) -> None:
        self.injected_client = async_supabase_client
        self.get_or_create_calls: List[Dict[str, Any]] = []
        FakeStore.instances.append(self)

    async def get_or_create(self, **kwargs: Any) -> str:
        self.get_or_create_calls.append(kwargs)
        if FakeStore.get_or_create_raises:
            raise RuntimeError("supabase down")
        return "conv-1"


class _Recorder:
    def __init__(self) -> None:
        self.runner_builds: List[Dict[str, Any]] = []
        self.audio_storage_calls = 0
        self.image_storage_calls = 0
        self.sent: List[str] = []
        self.send_ok = True
        self.send_raises = False


class FakeProvider:
    """Provider fake resolvido via ``resolve_provider`` — ``resolve_media_url`` puro.

    Espelha o contrato do z-api: a URL crua da ``MediaRef`` já é GETtable, então
    devolve ``resolved_url or raw_ref or stable_url`` sem I/O de rede.
    """

    def resolve_media_url(self, ref: Any) -> Optional[str]:
        return ref.resolved_url or ref.raw_ref or ref.stable_url


class FakeFacade:
    """Fachada de send fake (2-arg), ligada ao recorder.

    Espelha ``WhatsAppService.send_message(to_number, text)``: o provider já
    carrega a config, logo NÃO recebe ``integration``. Honra send_ok/send_raises.
    """

    def __init__(self, rec: _Recorder) -> None:
        self._rec = rec

    def send_message(self, phone: str, text: str) -> bool:
        if self._rec.send_raises:
            raise RuntimeError("provider down")
        self._rec.sent.append(text)
        return self._rec.send_ok


class FakeIntegrationService:
    """Resolver de tenant no modelo TOKEN: ``process_inbound`` resolve por
    ``get_integration_by_id(__edge_integration_id)`` (carimbo da borda token-only),
    NÃO mais por ``connectedPhone``. ``by_id_calls`` registra os ids resolvidos."""

    def __init__(self, integration: Optional[Dict[str, Any]]) -> None:
        self._integration = integration
        self.raise_on_lookup = False
        self.by_id_calls: List[str] = []

    def get_integration_by_id(
        self, integration_id: str
    ) -> Optional[Dict[str, Any]]:
        self.by_id_calls.append(integration_id)
        if self.raise_on_lookup:
            raise RuntimeError("boom arbitrário")
        return self._integration

    def get_or_create_user(
        self, *, phone: str, company_id: str, name: Optional[str]
    ) -> str:
        return "user-1"


class _FakeSyncSupabase:
    """get_supabase_client() fake: só precisa expor ``.client``."""

    def __init__(self) -> None:
        self.client = object()


# =========================================================================== #
# Harness
# =========================================================================== #
def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    event: Any,
    integration: Optional[Dict[str, Any]] = None,
) -> tuple[_Recorder, FakeRunner, FakeIntegrationService]:
    """Liga os colaboradores do MÓDULO do service a fakes."""
    rec = _Recorder()
    runner = FakeRunner(event)
    FakeAudioService.instances.clear()
    FakeAudioService.raise_on_transcribe = False
    FakeStore.instances.clear()
    FakeStore.get_or_create_raises = False

    if integration is None:
        integration = {
            "id": TEST_INTEGRATION_ID,
            "company_id": "co-1",
            "agent_id": "agent-1",
        }
    integration_service = FakeIntegrationService(integration)

    monkeypatch.setattr(wts, "get_supabase_client", lambda: _FakeSyncSupabase())
    monkeypatch.setattr(
        wts, "get_integration_service", lambda client: integration_service
    )
    monkeypatch.setattr(wts, "ConversationStore", FakeStore)
    monkeypatch.setattr(
        wts,
        "build_whatsapp_turn_runner",
        lambda **kw: (rec.runner_builds.append(kw) or runner),
    )
    monkeypatch.setattr(wts, "get_qdrant_service", lambda: None)

    async def _fake_audio_storage(url: str, company_id: str, client: Any) -> str:
        rec.audio_storage_calls += 1
        return "https://storage/audio.ogg"

    async def _fake_image_storage(url: str, company_id: str, client: Any) -> str:
        rec.image_storage_calls += 1
        return "https://storage/image.jpg"

    monkeypatch.setattr(wts, "process_audio_for_storage", _fake_audio_storage)
    monkeypatch.setattr(wts, "process_image_for_vision", _fake_image_storage)
    monkeypatch.setattr(wts, "AudioService", FakeAudioService)
    # Provider resolvido via registry + fachada injetada (substitui o
    # get_whatsapp_service/_make_whatsapp_sender legados).
    monkeypatch.setattr(wts, "resolve_provider", lambda integration: FakeProvider())
    monkeypatch.setattr(wts, "WhatsAppService", lambda provider: FakeFacade(rec))

    return rec, runner, integration_service


def _text_payload() -> Dict[str, Any]:
    return {
        "connectedPhone": "5511999999999",
        "phone": "5544888888888",
        "senderName": "Cliente",
        "text": {"message": "olá"},
        # Carimbo confiável da borda token-only (resolve o tenant por id).
        "__edge_integration_id": TEST_INTEGRATION_ID,
    }


def _audio_payload() -> Dict[str, Any]:
    return {
        "connectedPhone": "5511999999999",
        "phone": "5544888888888",
        "senderName": "Cliente",
        "audio": {"audioUrl": "https://z-api/audio.ogg"},
        "__edge_integration_id": TEST_INTEGRATION_ID,
    }


def _image_payload(caption: Optional[str] = "olha isso") -> Dict[str, Any]:
    image: Dict[str, Any] = {"imageUrl": "https://z-api/img.jpg"}
    if caption is not None:
        image["caption"] = caption
    return {
        "connectedPhone": "5511999999999",
        "phone": "5544888888888",
        "senderName": "Cliente",
        "image": image,
        "__edge_integration_id": TEST_INTEGRATION_ID,
    }


def _run(
    payload: Dict[str, Any],
    combined: Optional[str] = None,
    *,
    db: Optional[FakeAsyncSupabaseClient] = None,
) -> FakeAsyncSupabaseClient:
    db = db or FakeAsyncSupabaseClient()
    asyncio.run(
        wts.process_inbound(payload, combined, async_supabase_client=db)
    )
    return db


# =========================================================================== #
# Critério da Diagnosis — sem acoplamento ao router
# =========================================================================== #
def test_module_does_not_import_webhook_router() -> None:
    # Defesa contra ciclo service -> router (D4): o fonte do service não pode
    # referenciar app.api.webhook.
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(wts))
    imported: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert all(not mod.startswith("app.api") for mod in imported), imported


# =========================================================================== #
# Casos felizes
# =========================================================================== #
def test_text_payload_proceed_sends_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = FakePrepared()
    rec, runner, _ = _install(monkeypatch, event=TurnProceed(prepared=prepared))

    _run(_text_payload())

    assert runner.calls == 1
    assert runner.last_req.media_kind == "text"
    assert runner.last_req.user_message == "olá"
    assert prepared.run_calls == 1
    assert rec.sent == ["resposta-ia"]


def test_combined_message_is_single_text_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = FakePrepared()
    rec, runner, _ = _install(monkeypatch, event=TurnProceed(prepared=prepared))

    _run(_text_payload(), combined="msg1\nmsg2\nmsg3")

    assert runner.calls == 1  # OQ6: 1 batch coalescido = 1 turno
    assert runner.last_req.user_message == "msg1\nmsg2\nmsg3"
    assert runner.last_req.media_kind == "text"
    assert runner.last_req.audio_url is None
    assert runner.last_req.image_url is None
    assert rec.sent == ["resposta-ia"]


def test_audio_placeholder_preturn_transcript_only_after_proceed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = FakePrepared()
    rec, runner, _ = _install(monkeypatch, event=TurnProceed(prepared=prepared))

    _run(_audio_payload())

    # Pré-turno: placeholder + áudio bruto no storage, NUNCA transcrito.
    assert runner.last_req.media_kind == "audio"
    assert runner.last_req.user_message == "[Mensagem de voz]"
    assert runner.last_req.audio_url == "https://storage/audio.ogg"
    assert rec.audio_storage_calls == 1
    # Corpo: SÓ após TurnProceed roda com o texto TRANSCRITO (OQ12).
    assert len(FakeAudioService.instances) == 1
    assert FakeAudioService.instances[0].calls == 1
    assert prepared.last_req.user_message == "transcrição-real"
    assert prepared.last_req.media_kind == "text"
    assert prepared.last_req.audio_url is None
    assert prepared.last_req.persist_user_message is True


def test_audio_no_transcription_on_handoff(monkeypatch: pytest.MonkeyPatch) -> None:
    rec, runner, _ = _install(monkeypatch, event=TurnHandoff())

    _run(_audio_payload())

    assert rec.audio_storage_calls == 1  # storage p/ persistência do handoff
    assert FakeAudioService.instances == []  # Whisper NUNCA rodou
    assert rec.sent == []  # handoff -> bot silencioso


def test_audio_no_transcription_on_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    rec, runner, _ = _install(
        monkeypatch, event=TurnRejected(reason="INSUFFICIENT_BALANCE")
    )

    _run(_audio_payload())

    assert FakeAudioService.instances == []  # rejected: nada transcrito
    assert rec.sent == [COPY_INDISPONIVEL]


def test_image_with_caption(monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = FakePrepared()
    rec, runner, _ = _install(monkeypatch, event=TurnProceed(prepared=prepared))

    _run(_image_payload(caption="olha isso"))

    assert runner.last_req.media_kind == "image"
    assert runner.last_req.user_message == "olha isso"
    assert runner.last_req.image_url == "https://storage/image.jpg"
    assert prepared.last_req.image_url == "https://storage/image.jpg"
    assert rec.image_storage_calls == 1


def test_image_without_caption_uses_default_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = FakePrepared()
    rec, runner, _ = _install(monkeypatch, event=TurnProceed(prepared=prepared))

    _run(_image_payload(caption=None))

    assert runner.last_req.media_kind == "image"
    assert runner.last_req.user_message == "🖼️ [Imagem enviada]"
    assert rec.sent == ["resposta-ia"]


def test_integration_absent_silent_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    rec, runner, svc = _install(
        monkeypatch, event=TurnProceed(prepared=FakePrepared())
    )
    # Id carimbado existe, mas nenhuma linha casa (token revogado / linha inativa
    # entre o resolve da borda e o re-read por id) -> abort silencioso.
    monkeypatch.setattr(svc, "get_integration_by_id", lambda integration_id: None)

    _run(_text_payload())

    assert runner.calls == 0  # abort ANTES do runner
    assert rec.runner_builds == []
    assert rec.sent == []


def test_missing_edge_stamp_aborts_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAIL-CLOSED (SPEC §3.3): payload SEM ``__edge_integration_id`` (carimbo da
    borda token-only ausente) aborta o turno ANTES de qualquer resolução/escrita.
    NÃO há fallback por ``connectedPhone`` — não resolve tenant, não cria usuário,
    não roda runner, não envia. Trava o invariante de que só a borda token-only
    produz inbound carimbado.
    """
    rec, runner, svc = _install(
        monkeypatch, event=TurnProceed(prepared=FakePrepared())
    )

    payload = _text_payload()
    del payload["__edge_integration_id"]  # carimbo ausente
    _run(payload)

    # Tenant NUNCA resolvido (nem por id nem por phone) e nada downstream.
    assert svc.by_id_calls == []
    assert runner.calls == 0
    assert rec.runner_builds == []
    assert rec.sent == []


# =========================================================================== #
# Guard de números internos (§8.4 / S4 entregável 7)
# =========================================================================== #
def test_internal_number_blocked_aborts_before_user_and_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caso POSITIVO (§8.4): phone na blocklist NÃO cria user/conversation e NÃO
    roda runner. Exercita o SELECT REAL do guard (client com blocklist controlada),
    provando que o guard é invocado ANTES de get_or_create / resolve_pre_turn — e
    não passa por acidente no except silencioso de is_blocked.
    """
    prepared = FakePrepared()
    rec, runner, _ = _install(monkeypatch, event=TurnProceed(prepared=prepared))

    # phone do payload normalizado = '5544888888888' (já em E.164 sem '+').
    db = BlocklistAsyncClient(
        blocklist_rows=[
            {
                "id": "bl-1",
                "company_id": "co-1",
                "phone_normalized": "5544888888888",
                "active": True,
                "block_count": 0,
            }
        ]
    )
    _run(_text_payload(), db=db)

    # Abortou ANTES de qualquer escrita de domínio / runner.
    assert FakeStore.instances[0].get_or_create_calls == []
    assert runner.calls == 0
    assert rec.runner_builds == []
    assert rec.sent == []  # bot silencioso (não responde número interno)
    # Efeito colateral do bloqueio: block_count incrementado na linha.
    assert db.tables["internal_whatsapp_blocklist"][0]["block_count"] == 1
    assert db.tables["internal_whatsapp_blocklist"][0]["last_blocked_at"]


def test_non_listed_number_follows_normal_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caso NEGATIVO (§8.4): phone NÃO listado segue o turno normal (cria
    user/conversation, roda runner, responde). Blocklist com outro número apenas —
    o guard executa o SELECT real e NÃO bloqueia o cliente legítimo.
    """
    prepared = FakePrepared()
    rec, runner, _ = _install(monkeypatch, event=TurnProceed(prepared=prepared))

    db = BlocklistAsyncClient(
        blocklist_rows=[
            {
                "id": "bl-9",
                "company_id": "co-1",
                "phone_normalized": "5500000000000",  # outro número
                "active": True,
                "block_count": 0,
            }
        ]
    )
    _run(_text_payload(), db=db)

    assert len(FakeStore.instances[0].get_or_create_calls) == 1
    assert runner.calls == 1
    assert prepared.run_calls == 1
    assert rec.sent == ["resposta-ia"]
    # Nenhum incremento de bloqueio para o número legítimo.
    assert db.tables["internal_whatsapp_blocklist"][0]["block_count"] == 0


def test_rejected_sends_canonical_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    rec, runner, _ = _install(
        monkeypatch, event=TurnRejected(reason="BILLING_UNAVAILABLE")
    )

    _run(_text_payload())

    assert rec.sent == [COPY_INDISPONIVEL]


def test_handoff_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    rec, runner, _ = _install(monkeypatch, event=TurnHandoff())

    _run(_text_payload())

    assert runner.calls == 1
    assert rec.sent == []


# =========================================================================== #
# D5 — wiring do client async real (store por chamada + factory)
# =========================================================================== #
def test_store_built_per_call_with_injected_async_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec, runner, _ = _install(monkeypatch, event=TurnProceed(prepared=FakePrepared()))

    db = _run(_text_payload())

    # ConversationStore construído POR CHAMADA com o client injetado (D5).
    assert len(FakeStore.instances) == 1
    assert FakeStore.instances[0].injected_client is db
    assert len(FakeStore.instances[0].get_or_create_calls) == 1
    extra = FakeStore.instances[0].get_or_create_calls[0]["extra_fields"]
    assert set(extra) == {"user_name", "user_phone", "agent_name", "status_color"}

    # Factory recebe o `.client` CRU do wrapper (V2/OQ-2, espelha chat.py:357).
    assert len(rec.runner_builds) == 1
    build = rec.runner_builds[0]
    assert build["company_id"] == "co-1"
    assert build["agent_id"] == "agent-1"
    assert build["async_supabase_client"] is db.client


# =========================================================================== #
# Casos de erro do contrato §5.1
# =========================================================================== #
def test_a_never_raise_arbitrary_exception_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec, runner, svc = _install(
        monkeypatch, event=TurnProceed(prepared=FakePrepared())
    )
    svc.raise_on_lookup = True  # exceção arbitrária no meio do pipeline

    # NÃO pode propagar ao caller (never-raise, catch-all final).
    _run(_text_payload())

    assert runner.calls == 0
    assert rec.sent == []


def test_b_payload_without_valid_content_returns_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec, runner, _ = _install(monkeypatch, event=TurnProceed(prepared=FakePrepared()))

    payload = {
        "connectedPhone": "5511999999999",
        "phone": "5544888888888",
        "senderName": "Cliente",
        "__edge_integration_id": TEST_INTEGRATION_ID,
    }
    _run(payload)

    assert runner.calls == 0  # sem turno
    assert rec.runner_builds == []
    assert rec.sent == []  # sem envio


def test_c_get_or_create_failure_does_not_abort_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = FakePrepared()
    rec, runner, _ = _install(monkeypatch, event=TurnProceed(prepared=prepared))
    FakeStore.get_or_create_raises = True

    _run(_text_payload())

    # warning + SEGUE: o runner é construído e o turno roda normalmente.
    assert runner.calls == 1
    assert prepared.run_calls == 1
    assert rec.sent == ["resposta-ia"]


def test_d_whisper_failure_after_proceed_sends_exact_copy_and_skips_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = FakePrepared()
    rec, runner, _ = _install(monkeypatch, event=TurnProceed(prepared=prepared))
    FakeAudioService.raise_on_transcribe = True

    _run(_audio_payload())

    assert rec.sent == ["Erro ao processar áudio."]  # cópia EXATA
    assert prepared.run_calls == 0  # corpo do turno NÃO roda


def test_e_sender_raising_does_not_reinvoke_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = FakePrepared()
    rec, runner, _ = _install(monkeypatch, event=TurnProceed(prepared=prepared))
    rec.send_raises = True

    _run(_text_payload())

    assert prepared.run_calls == 1  # corpo rodou UMA vez, sem regeneração


def test_e_sender_returning_false_does_not_reinvoke_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = FakePrepared()
    rec, runner, _ = _install(monkeypatch, event=TurnProceed(prepared=prepared))
    rec.send_ok = False

    _run(_text_payload())

    assert prepared.run_calls == 1
    assert rec.sent == ["resposta-ia"]  # entregue ao sender, que reportou falha
