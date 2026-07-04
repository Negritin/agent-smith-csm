"""Characterization tests for EvolutionProvider (Bridge Evolution API v2, NOVO).

SPEC — Sprint "Bridge Evolution API v2 (NOVO) com validacao de doc".

Locks the Evolution API v2 wire for the NEW provider (Baileys-like sibling of
uazapi, ZERO legacy tenant — a wrong shape stays CONTAINED here):

  - validate_config: base_url/instance_id/token mandatory; client_token ignored;
  - send_text  -> POST {base}/message/sendText/{instance} body {"number","text"}
    header apikey;
  - send_media(image) -> /message/sendMedia/{instance}
    {"number","mediatype":"image","media",(+caption,+mimetype)};
    send_media(audio)  -> /message/sendWhatsAppAudio/{instance}
    {"number","audio"}; missing url -> TERMINAL ok=False;
  - contrato neutro: 2xx -> ok=True; 429/5xx -> WhatsappRetryableError;
    other -> ok=False;
  - parse_webhook -> messages.upsert -> InboundBatch neutro DIRETO (text);
    statuses=[] SEMPRE;
  - resolve_media_url -> passthrough de URL já-GETtable; None caso contrário
    (sem invenção de wire de mídia inbound — NI-03);
  - SSRF/allowlist/cap: cobertos via o fluxo inbound real
    (process_image_for_vision / process_audio_for_storage) com EVOLUTION host;
  - capabilities all False; send_template raises ProviderNotSupportedError.

=============================================================================
EVIDÊNCIA VERSIONADA — Evolution API v2 (fonte da verdade do fio deste bridge)
=============================================================================
Data de consulta: 2026-06-25.

[1] Referência oficial v2 (CANÔNICA) — send text:
    https://doc.evolution-api.com/v2/api-reference/message-controller/send-text
    Exemplo aprovado (body literal):
        POST {server}/message/sendText/{instance}
        headers: apikey: <key> ; Content-Type: application/json
        body: { "number": "5511999999999", "text": "Hello from Evolution API v2!" }

[2] Manual de Integração Evolution API V2 (corrobora os MESMOS shapes):
    https://gist.github.com/dantetesta/b8b7e7e2d6196beae968c8b0a61afb7a
    - sendText  -> POST /message/sendText/{instanceName}
        body: { "number": "5511999999999", "text": "Olá! ..." }
    - sendMedia (imagem, forma URL) -> POST /message/sendMedia/{instanceName}
        body: { "number":"5511999999999", "mediatype":"image",
                "media":"https://.../foto.jpg", "fileName":"foto.jpg",
                "caption":"Legenda da imagem", "mimetype":"image/jpeg" }
    - sendWhatsAppAudio -> POST /message/sendWhatsAppAudio/{instanceName}
        body: { "number":"5511999999999", "audio":"<url-ou-base64>" }
    - webhook messages.upsert (envelope):
        { "event":"messages.upsert", "instance":"...",
          "data": { "key": {"remoteJid":"...@s.whatsapp.net","fromMe":true,"id":"3EB0..."},
                    "message": {"conversation":"...","extendedTextMessage":{"text":"..."}},
                    "messageTimestamp":"1234567890" } }

[3] Webhook envelope confirmado contra a doc oficial de webhooks v2:
    https://doc.evolution-api.com/v2/en/configuration/webhooks
    (event + instance no topo; key/message/messageTimestamp dentro de data).

DIVERGÊNCIA SINALIZADA (resolvida por EVIDÊNCIA, não por chute):
    O mirror https://docs.evolutionfoundation.com.br/evolution-api/send-text-message
    mostra o body estilo v1 { "number", "textMessage": { "text" } }. As fontes
    [1] e [2] (referência v2 oficial + manual v2) mostram o body FLAT
    { "number", "text" }. Este bridge usa { "number", "text" } (v2). O mirror
    evolutionfoundation é o shape v1 STALE e NÃO se aplica à v2.

NÃO CONFIRMADO / NÃO INVENTADO (NI-03):
    Parsing de MÍDIA inbound (resolução de referência -> URL GETtable) NÃO foi
    confirmado na doc v2 (Evolution entrega base64 / /chat/getBase64FromMediaMessage,
    que retorna base64, não URL). resolve_media_url só faz passthrough de URL
    http(s) já-baixável e retorna None caso contrário; parse_webhook classifica
    não-texto como type='unknown'. Quando o shape for confirmado contra instância
    real, estender no provider — a divergência fica contida no arquivo.

Convenções espelham test_uazapi_provider.py: sem pytest-asyncio nos testes do
provider (métodos sync; requests.post monkeypatched); os testes de SSRF/cap usam
asyncio.run contra o fluxo inbound real (espelham test_webhook_ssrf_uazapi.py).
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest
import requests

import app.services.whatsapp.providers.evolution as evolution_mod
import app.services.whatsapp_turn_service as webhook
from app.core.security.url_validator import ValidatedExternalUrl
from app.services.whatsapp.exceptions import (
    ProviderConfigError,
    ProviderNotSupportedError,
    WhatsappRetryableError,
)
from app.services.whatsapp.models import MediaRef, OutboundMedia, TemplateRef
from app.services.whatsapp.providers.base import WhatsAppProvider
from app.services.whatsapp.providers.evolution import EvolutionProvider


INTEGRATION: Dict[str, Any] = {
    "provider": "evolution",
    "base_url": "https://evo.example.com",
    "instance_id": "my-instance",
    "token": "evo-apikey-1",
}


# =========================================================================== #
# Fakes (espelham test_uazapi_provider.py)
# =========================================================================== #
class FakeResponse:
    def __init__(self, status_code: int, *, text: str = "", payload: Any = None) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error")

    def json(self) -> Any:
        return self._payload


class FakePost:
    def __init__(self, sequence: List[Any]) -> None:
        self._sequence = list(sequence)
        self.calls = 0
        self.urls: List[str] = []
        self.bodies: List[Any] = []
        self.headers: List[Any] = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls += 1
        self.urls.append(url)
        self.bodies.append(json)
        self.headers.append(headers)
        idx = min(self.calls - 1, len(self._sequence) - 1)
        item = self._sequence[idx]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, FakeResponse):
            return item
        return FakeResponse(item)


def _install(monkeypatch: pytest.MonkeyPatch, sequence: List[Any]) -> FakePost:
    fake = FakePost(sequence)
    monkeypatch.setattr(evolution_mod.requests, "post", fake)
    return fake


def _provider(**overrides: Any) -> EvolutionProvider:
    cfg = dict(INTEGRATION)
    cfg.update(overrides)
    return EvolutionProvider(cfg)


# =========================================================================== #
# Protocol + capabilities + validate_config
# =========================================================================== #
def test_implements_protocol() -> None:
    assert isinstance(_provider(), WhatsAppProvider)


def test_capabilities_all_false() -> None:
    caps = _provider().capabilities
    assert caps.templates is False
    assert caps.session_window_24h is False
    assert caps.delivery_statuses is False
    assert caps.media_by_id is False
    assert caps.hmac_webhook is False
    assert caps.interactive is False


def test_validate_config_missing_base_url_raises() -> None:
    with pytest.raises(ProviderConfigError):
        EvolutionProvider({"instance_id": "i", "token": "t"})


def test_validate_config_missing_instance_id_raises() -> None:
    with pytest.raises(ProviderConfigError):
        EvolutionProvider({"base_url": "https://e", "token": "t"})


def test_validate_config_missing_token_raises() -> None:
    with pytest.raises(ProviderConfigError):
        EvolutionProvider({"base_url": "https://e", "instance_id": "i"})


def test_validate_config_ignores_client_token() -> None:
    # client_token é ignorado (NULL p/ Evolution): config válida sem ele.
    provider = EvolutionProvider(
        {
            "base_url": "https://e",
            "instance_id": "i",
            "token": "t",
            "client_token": "ignored",
        }
    )
    assert isinstance(provider, EvolutionProvider)


# =========================================================================== #
# send_text — endpoint, body {number,text}, header apikey (EVIDÊNCIA [1]/[2])
# =========================================================================== #
def test_send_text_url_body_apikey_header(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, [200])

    result = _provider().send_text("5511999999999", "Hello from Evolution API v2!")

    assert result.ok is True
    assert fake.urls[0] == "https://evo.example.com/message/sendText/my-instance"
    # body FLAT v2 (NÃO textMessage:{text}) — resolvido por evidência.
    assert fake.bodies[0] == {
        "number": "5511999999999",
        "text": "Hello from Evolution API v2!",
    }
    assert fake.headers[0] == {
        "Content-Type": "application/json",
        "apikey": "evo-apikey-1",
    }


def test_send_text_body_is_not_v1_textmessage_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Guard explícito contra regressão para o shape v1 do mirror evolutionfoundation.
    fake = _install(monkeypatch, [200])
    _provider().send_text("5511999999999", "oi")
    assert "textMessage" not in fake.bodies[0]
    assert fake.bodies[0]["text"] == "oi"


def test_send_text_5xx_raises_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [500])
    with pytest.raises(WhatsappRetryableError):
        _provider().send_text("5511999999999", "oi")


def test_send_text_429_raises_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [429])
    with pytest.raises(WhatsappRetryableError):
        _provider().send_text("5511999999999", "oi")


def test_send_text_terminal_4xx_returns_not_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, [400])
    result = _provider().send_text("5511999999999", "oi")
    assert result.ok is False
    assert result.error == "HTTP 400"


# =========================================================================== #
# send_media — image (sendMedia) / audio (sendWhatsAppAudio) (EVIDÊNCIA [2])
# =========================================================================== #
def test_send_media_image_body(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, [200])

    media = OutboundMedia(
        kind="image",
        url="https://s/i.jpg",
        caption="Legenda da imagem",
        mime_type="image/jpeg",
    )
    result = _provider().send_media("5511999999999", media)

    assert result.ok is True
    assert fake.urls[0] == "https://evo.example.com/message/sendMedia/my-instance"
    assert fake.bodies[0] == {
        "number": "5511999999999",
        "mediatype": "image",
        "media": "https://s/i.jpg",
        "caption": "Legenda da imagem",
        "mimetype": "image/jpeg",
    }
    assert fake.headers[0]["apikey"] == "evo-apikey-1"


def test_send_media_image_without_caption_or_mimetype(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, [200])
    _provider().send_media("5511999999999", OutboundMedia(kind="image", url="https://s/i.jpg"))
    # caption/mimetype só são declarados quando suportados/presentes.
    assert fake.bodies[0] == {
        "number": "5511999999999",
        "mediatype": "image",
        "media": "https://s/i.jpg",
    }


def test_send_media_audio_body(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, [200])

    media = OutboundMedia(kind="audio", url="https://s/a.ogg")
    result = _provider().send_media("5511999999999", media)

    assert result.ok is True
    assert fake.urls[0] == "https://evo.example.com/message/sendWhatsAppAudio/my-instance"
    assert fake.bodies[0] == {"number": "5511999999999", "audio": "https://s/a.ogg"}


def test_send_media_missing_url_terminal_no_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, [200])
    # media_by_id=False -> raw_ref não é usável; sem url => TERMINAL ok=False.
    result = _provider().send_media(
        "5511999999999", OutboundMedia(kind="image", raw_ref="MEDIA_ID_1")
    )
    assert result.ok is False
    assert fake.calls == 0  # nenhum POST disparado


def test_send_media_5xx_raises_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [503])
    with pytest.raises(WhatsappRetryableError):
        _provider().send_media(
            "5511999999999", OutboundMedia(kind="audio", url="https://s/a.ogg")
        )


# =========================================================================== #
# send_template — não suportado
# =========================================================================== #
def test_send_template_raises_not_supported() -> None:
    with pytest.raises(ProviderNotSupportedError):
        _provider().send_template("5511999999999", TemplateRef(name="welcome"))


# =========================================================================== #
# parse_webhook — messages.upsert (texto) -> InboundBatch neutro (EVIDÊNCIA [2]/[3])
# =========================================================================== #
def _upsert(
    *,
    data: Dict[str, Any],
    event: str = "messages.upsert",
    instance: str = "my-instance",
) -> Dict[str, Any]:
    return {"event": event, "instance": instance, "data": data}


def test_parse_webhook_conversation_text() -> None:
    batch = _provider().parse_webhook(
        _upsert(
            data={
                "key": {
                    "remoteJid": "5544999999999@s.whatsapp.net",
                    "fromMe": False,
                    "id": "3EB0ABC",
                },
                "message": {"conversation": "olá mundo"},
                "messageTimestamp": "1716200000",
                "pushName": "Cliente A",
            }
        )
    )
    assert batch.provider == "evolution"
    assert batch.statuses == []
    assert len(batch.messages) == 1

    msg = batch.messages[0]
    assert msg.type == "text"
    assert msg.text == "olá mundo"
    assert msg.from_phone == "5544999999999"  # JID stripado
    assert msg.from_me is False
    assert msg.is_group is False
    assert msg.message_id == "3EB0ABC"
    assert msg.timestamp == 1716200000
    assert msg.sender_name == "Cliente A"
    assert msg.media is None


def test_parse_webhook_extended_text_message() -> None:
    batch = _provider().parse_webhook(
        _upsert(
            data={
                "key": {
                    "remoteJid": "5544999999999@s.whatsapp.net",
                    "fromMe": False,
                    "id": "ID-2",
                },
                "message": {"extendedTextMessage": {"text": "via extended"}},
            }
        )
    )
    assert batch.messages[0].type == "text"
    assert batch.messages[0].text == "via extended"


def test_parse_webhook_from_me_true() -> None:
    batch = _provider().parse_webhook(
        _upsert(
            data={
                "key": {
                    "remoteJid": "5544999999999@s.whatsapp.net",
                    "fromMe": True,
                    "id": "X",
                },
                "message": {"conversation": "eco"},
            }
        )
    )
    assert batch.messages[0].from_me is True


def test_parse_webhook_group_uses_participant() -> None:
    batch = _provider().parse_webhook(
        _upsert(
            data={
                "key": {
                    "remoteJid": "120363000000000000@g.us",
                    "participant": "5544888888888@s.whatsapp.net",
                    "fromMe": False,
                    "id": "G-1",
                },
                "message": {"conversation": "no grupo"},
            }
        )
    )
    msg = batch.messages[0]
    assert msg.is_group is True
    assert msg.from_phone == "5544888888888"  # remetente, não o JID do grupo


def test_parse_webhook_connected_phone_from_instance() -> None:
    batch = _provider().parse_webhook(
        _upsert(
            data={
                "key": {"remoteJid": "5544999999999@s.whatsapp.net", "id": "I"},
                "message": {"conversation": "x"},
            },
            instance="tenant-line-1",
        )
    )
    assert batch.connected_phone == "tenant-line-1"
    assert batch.messages[0].connected_phone == "tenant-line-1"


def test_parse_webhook_connected_phone_prefers_owner() -> None:
    batch = _provider().parse_webhook(
        _upsert(
            data={
                "owner": "5511777777777",
                "key": {"remoteJid": "5544999999999@s.whatsapp.net", "id": "I"},
                "message": {"conversation": "x"},
            },
            instance="tenant-line-1",
        )
    )
    assert batch.connected_phone == "5511777777777"


@pytest.mark.parametrize(
    "event",
    ["messages.update", "connection.update", "presence.update", "contacts.upsert"],
)
def test_parse_webhook_non_upsert_event_empty_batch(event: str) -> None:
    batch = _provider().parse_webhook(
        _upsert(
            data={
                "key": {"remoteJid": "5544999999999@s.whatsapp.net", "id": "I"},
                "message": {"conversation": "x"},
            },
            event=event,
        )
    )
    assert batch.messages == []
    assert batch.statuses == []


@pytest.mark.parametrize("event", ["messages.upsert", "MESSAGES.UPSERT", "Messages.Upsert"])
def test_parse_webhook_upsert_case_insensitive(event: str) -> None:
    batch = _provider().parse_webhook(
        _upsert(
            data={
                "key": {"remoteJid": "5544999999999@s.whatsapp.net", "id": "I"},
                "message": {"conversation": "x"},
            },
            event=event,
        )
    )
    assert len(batch.messages) == 1


def test_parse_webhook_no_message_empty_batch() -> None:
    batch = _provider().parse_webhook(
        _upsert(data={"key": {"remoteJid": "5544999999999@s.whatsapp.net", "id": "I"}})
    )
    assert batch.messages == []
    assert batch.statuses == []


def test_parse_webhook_no_text_yields_unknown() -> None:
    # Mídia/sticker inbound: shape NÃO confirmado -> type='unknown' (não dropa).
    batch = _provider().parse_webhook(
        _upsert(
            data={
                "key": {"remoteJid": "5544999999999@s.whatsapp.net", "id": "I"},
                "message": {"imageMessage": {"url": "enc://whatsapp"}},
            }
        )
    )
    assert len(batch.messages) == 1
    assert batch.messages[0].type == "unknown"
    assert batch.messages[0].text is None
    assert batch.messages[0].media is None


def test_parse_webhook_no_provider_field_leaks() -> None:
    batch = _provider().parse_webhook(
        _upsert(
            data={
                "key": {"remoteJid": "5544999999999@s.whatsapp.net", "id": "I"},
                "message": {"conversation": "x"},
            }
        )
    )
    assert not hasattr(batch, "_provider")
    assert not hasattr(batch.messages[0], "_provider")


def test_parse_webhook_tolerates_extra_keys() -> None:
    batch = _provider().parse_webhook(
        {
            "event": "messages.upsert",
            "instance": "my-instance",
            "destination": "https://wh",
            "date_time": "2026-06-25T00:00:00Z",
            "unknownTop": 1,
            "data": {
                "key": {
                    "remoteJid": "5544999999999@s.whatsapp.net",
                    "fromMe": False,
                    "id": "I",
                    "unknownKey": "z",
                },
                "message": {"conversation": "x", "messageContextInfo": {}},
                "messageType": "conversation",
                "extraneous": True,
            },
        }
    )
    assert batch.messages[0].text == "x"


def test_parse_webhook_non_numeric_timestamp_is_none() -> None:
    batch = _provider().parse_webhook(
        _upsert(
            data={
                "key": {"remoteJid": "5544999999999@s.whatsapp.net", "id": "I"},
                "message": {"conversation": "x"},
                "messageTimestamp": "not-a-number",
            }
        )
    )
    assert batch.messages[0].timestamp is None


def test_parse_webhook_flat_payload_tolerated() -> None:
    # Forward-compat: payload já no formato data (sem envelope) é tolerado.
    batch = _provider().parse_webhook(
        {
            "key": {"remoteJid": "5544999999999@s.whatsapp.net", "id": "I"},
            "message": {"conversation": "flat"},
        }
    )
    assert len(batch.messages) == 1
    assert batch.messages[0].text == "flat"


# =========================================================================== #
# resolve_media_url — passthrough http(s); None caso contrário (NI-03)
# =========================================================================== #
def test_resolve_media_url_passthrough_resolved_url() -> None:
    out = _provider().resolve_media_url(
        MediaRef(kind="image", resolved_url="https://cdn.evo.example.com/x.jpg")
    )
    assert out == "https://cdn.evo.example.com/x.jpg"


def test_resolve_media_url_passthrough_raw_ref_http() -> None:
    out = _provider().resolve_media_url(
        MediaRef(kind="audio", raw_ref="http://cdn.evo.example.com/a.ogg")
    )
    assert out == "http://cdn.evo.example.com/a.ogg"


def test_resolve_media_url_none_for_non_url_ref() -> None:
    # Referência base64/opaca NÃO é uma URL baixável -> None (sem crash, sem
    # invenção de endpoint de resolução).
    assert _provider().resolve_media_url(MediaRef(kind="image", raw_ref="MEDIA_ID_1")) is None


def test_resolve_media_url_none_when_empty() -> None:
    assert _provider().resolve_media_url(MediaRef(kind="audio")) is None


# =========================================================================== #
# verify_webhook — sem HMAC, retorna True
# =========================================================================== #
def test_verify_webhook_returns_true() -> None:
    assert _provider().verify_webhook({}, "") is True


# =========================================================================== #
# SSRF / allowlist / cap — via o fluxo inbound REAL (espelha test_webhook_ssrf_uazapi)
# resolve_media_url devolve uma URL GETtable; a borda inbound a valida.
# =========================================================================== #
EVOLUTION_HOST = "media.evo.example.com"
EVOLUTION_MEDIA_URL = f"https://{EVOLUTION_HOST}/audio.ogg"

BLOCKED_EVOLUTION_URLS = [
    "https://127.0.0.1/media.ogg",          # loopback
    "https://10.0.0.5/media.ogg",           # privado
    "https://169.254.169.254/latest/meta",  # link-local metadata
    "https://[::1]/media.ogg",              # ipv6 loopback
    "http://media.evo.example.com/a.ogg",   # esquema não-https
]


class _ExplodingAsyncClient:
    """httpx.AsyncClient que NÃO pode ser instanciado (prova: nenhum GET)."""

    instantiated = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _ExplodingAsyncClient.instantiated = True
        raise AssertionError("outbound httpx client must NOT be created for a blocked URL")


class _FakeStreamResponse:
    def __init__(self, total_bytes: int, chunk: int = 256 * 1024) -> None:
        self._total = total_bytes
        self._chunk = chunk

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self):
        remaining = self._total
        while remaining > 0:
            n = min(self._chunk, remaining)
            remaining -= n
            yield b"\x00" * n

    async def aclose(self) -> None:
        return None

    async def __aenter__(self) -> "_FakeStreamResponse":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeCappedClient:
    def __init__(self, total_bytes: int, **kwargs: Any) -> None:
        self._total = total_bytes
        self.kwargs = kwargs

    def stream(self, method: str, url: str) -> _FakeStreamResponse:
        return _FakeStreamResponse(self._total)

    async def __aenter__(self) -> "_FakeCappedClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _Storage:
    def __init__(self, uploads: List[Any]) -> None:
        self._uploads = uploads

    def from_(self, *_a: Any):
        uploads = self._uploads

        class _B:
            def upload(self_inner, *a: Any, **k: Any) -> None:
                uploads.append(a)

            def get_public_url(self_inner, *a: Any) -> str:
                return "https://public/url.ogg"

        return _B()


class _Client:
    def __init__(self, uploads: List[Any]) -> None:
        self.storage = _Storage(uploads)


def _reset_allowlists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webhook.settings, "ZAPI_MEDIA_HOST_ALLOWLIST", "", raising=False)
    monkeypatch.setattr(webhook.settings, "UAZAPI_MEDIA_HOST_ALLOWLIST", "", raising=False)
    monkeypatch.setattr(
        webhook.settings, "EVOLUTION_MEDIA_HOST_ALLOWLIST", "", raising=False
    )


def _patch_validator_pass(monkeypatch: pytest.MonkeyPatch, host: str = EVOLUTION_HOST) -> None:
    validated = ValidatedExternalUrl(
        original_url=f"https://{host}/audio.ogg",
        normalized_url=f"https://{host}/audio.ogg",
        hostname=host,
        resolved_addresses=("93.184.216.34",),
    )
    monkeypatch.setattr(webhook, "validate_external_url", lambda url: validated)
    monkeypatch.setattr(webhook, "revalidate_external_url", lambda v: v)


def test_evolution_image_helper_blocks_ssrf_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A URL "resolvida" por resolve_media_url ainda passa pela borda SSRF: hosts
    # privados/loopback/metadata/http são bloqueados ANTES de qualquer GET.
    _ExplodingAsyncClient.instantiated = False
    monkeypatch.setattr(webhook.httpx, "AsyncClient", _ExplodingAsyncClient)
    _reset_allowlists(monkeypatch)

    for url in BLOCKED_EVOLUTION_URLS:
        assert _provider().resolve_media_url(MediaRef(kind="audio", raw_ref=url)) == url
        result = asyncio.run(
            webhook.process_image_for_vision(url, "co-evo", object())
        )
        assert result is None, f"expected block for {url}"

    assert _ExplodingAsyncClient.instantiated is False


def test_evolution_audio_over_5mb_aborted_no_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_validator_pass(monkeypatch)
    _reset_allowlists(monkeypatch)
    uploads: List[Any] = []
    monkeypatch.setattr(
        webhook.httpx, "AsyncClient", lambda **kw: _FakeCappedClient(6 * 1024 * 1024, **kw)
    )

    result = asyncio.run(
        webhook.process_audio_for_storage(EVOLUTION_MEDIA_URL, "co-evo", _Client(uploads))
    )

    assert result is None  # cap de 5 MB abortou o streaming
    assert uploads == []  # nunca bufferizou/uploadou


def test_evolution_allowlist_admits_listed_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Só a allowlist EVOLUTION setada com o host evolution -> PASSA.
    _patch_validator_pass(monkeypatch)
    monkeypatch.setattr(webhook.settings, "ZAPI_MEDIA_HOST_ALLOWLIST", "", raising=False)
    monkeypatch.setattr(webhook.settings, "UAZAPI_MEDIA_HOST_ALLOWLIST", "", raising=False)
    monkeypatch.setattr(
        webhook.settings, "EVOLUTION_MEDIA_HOST_ALLOWLIST", EVOLUTION_HOST, raising=False
    )
    uploads: List[Any] = []
    monkeypatch.setattr(
        webhook.httpx, "AsyncClient", lambda **kw: _FakeCappedClient(1024, **kw)
    )

    result = asyncio.run(
        webhook.process_image_for_vision(
            f"https://{EVOLUTION_HOST}/img.jpg", "co-evo", _Client(uploads)
        )
    )

    assert result == "https://public/url.ogg"
    assert len(uploads) == 1


def test_evolution_allowlist_rejects_unlisted_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Allowlist evolution setada com OUTRO host -> o host do payload é bloqueado
    # (validator OK, mas fora da união) — nenhum GET.
    _patch_validator_pass(monkeypatch)
    monkeypatch.setattr(webhook.settings, "ZAPI_MEDIA_HOST_ALLOWLIST", "", raising=False)
    monkeypatch.setattr(webhook.settings, "UAZAPI_MEDIA_HOST_ALLOWLIST", "", raising=False)
    monkeypatch.setattr(
        webhook.settings, "EVOLUTION_MEDIA_HOST_ALLOWLIST", "other.evo.net", raising=False
    )
    monkeypatch.setattr(webhook.httpx, "AsyncClient", _ExplodingAsyncClient)

    result = asyncio.run(
        webhook.process_image_for_vision(
            f"https://{EVOLUTION_HOST}/img.jpg", "co-evo", object()
        )
    )

    assert result is None  # host fora da allowlist unida -> bloqueado, sem GET


def test_zapi_non_regression_blocked_when_only_evolution_list_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # NÃO-REGRESSÃO de escopo: se SÓ a lista evolution está setada, um host
    # z-api fora dela é BLOQUEADO (a união não o inclui).
    _patch_validator_pass(monkeypatch, host="cdn.z-api.io")
    monkeypatch.setattr(webhook.settings, "ZAPI_MEDIA_HOST_ALLOWLIST", "", raising=False)
    monkeypatch.setattr(webhook.settings, "UAZAPI_MEDIA_HOST_ALLOWLIST", "", raising=False)
    monkeypatch.setattr(
        webhook.settings, "EVOLUTION_MEDIA_HOST_ALLOWLIST", EVOLUTION_HOST, raising=False
    )
    monkeypatch.setattr(webhook.httpx, "AsyncClient", _ExplodingAsyncClient)

    result = asyncio.run(
        webhook.process_image_for_vision("https://cdn.z-api.io/x.jpg", "co-zapi", object())
    )

    assert result is None  # host z-api não está na união -> bloqueado
