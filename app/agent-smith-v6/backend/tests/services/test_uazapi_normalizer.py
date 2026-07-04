"""V1 — Normalizador inbound uazapi (SPEC §3.5 / §9 V1).

Exercita ``normalize_uazapi_to_canonical`` + os models uazapi
(``UazapiWebhookEvent``/``UazapiInnerMessage``/``UazapiMessageKey``) declarados
em ``whatsapp_turn_service`` (SPEC §3.1). O normalizador é a ÚNICA peça que
conhece os dois mundos: converte o ``WebhookEvent`` uazapi para um dict canônico
de shape IDÊNTICO ao ``ZAPIWebhookPayload`` (mais a chave privada ``_provider``).

Cobertura (§9 V1.1–V1.8):
  V1.1 texto + strip de JID -> phone E.164; text.message; audio/image None.
  V1.2 áudio (messageType "audio" e "ptt") -> audio.audioUrl; imagem -> imageUrl/caption.
  V1.3 fromMe via message.fromMe / message.key.fromMe / *Api -> fromMe True (anti-loop).
  V1.4 grupo (@g.us ou isGroup) -> isGroup True; phone do remetente, NÃO do grupo.
  V1.5 cascatas connectedPhone / messageId (id estável) / senderName.
  V1.6 gate de event-type: updates/presence/connection/contacts/chats -> None;
       "messages" e "message" (singular, case-insensitive) -> dict válido.
  V1.7 evento sem `message` -> None.
  V1.8 paridade de shape: o dict (sem _provider) valida em ZAPIWebhookPayload e
       carrega exatamente as chaves consumidas; _provider == "uazapi".

Convenções (espelham as demais suítes de services): sem pytest-asyncio (o
normalizador é síncrono); asserts simples; env semeada por
tests/services/conftest.py ANTES de importar app.*.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import app.services.whatsapp_turn_service as wts
from app.services.whatsapp_turn_service import (
    UazapiWebhookEvent,
    ZAPIWebhookPayload,
    normalize_uazapi_to_canonical,
)


# =========================================================================== #
# Helpers
# =========================================================================== #
def _event(
    *,
    message: Optional[Dict[str, Any]],
    event: Optional[str] = "messages",
    EventType: Optional[str] = None,
    connectedPhone: Optional[str] = "5511999999999",
    owner: Optional[str] = None,
    instanceName: Optional[str] = None,
) -> UazapiWebhookEvent:
    raw: Dict[str, Any] = {"message": message}
    if event is not None:
        raw["event"] = event
    if EventType is not None:
        raw["EventType"] = EventType
    if connectedPhone is not None:
        raw["connectedPhone"] = connectedPhone
    if owner is not None:
        raw["owner"] = owner
    if instanceName is not None:
        raw["instanceName"] = instanceName
    return UazapiWebhookEvent(**raw)


# =========================================================================== #
# V1.1 — texto + strip de JID
# =========================================================================== #
def test_v1_1_text_message_strips_jid_and_validates() -> None:
    ev = _event(
        message={
            "chatid": "554499999999@s.whatsapp.net",
            "messageType": "conversation",
            "text": "olá mundo",
            "messageid": "MSG-1",
        }
    )
    canonical = normalize_uazapi_to_canonical(ev)
    assert canonical is not None

    # phone com strip do sufixo @s.whatsapp.net
    assert canonical["phone"] == "554499999999"
    assert canonical["text"] == {"message": "olá mundo"}
    assert canonical["audio"] is None
    assert canonical["image"] is None

    # valida no contrato canônico (sem a chave privada _provider)
    validatable = {k: v for k, v in canonical.items() if k != "_provider"}
    payload = ZAPIWebhookPayload(**validatable)
    assert payload.phone == "554499999999"
    assert payload.text is not None and payload.text.message == "olá mundo"
    assert payload.audio is None and payload.image is None


def test_v1_1_text_via_content_fallback() -> None:
    # `content` é o fallback de `text` (§3.5 / tabela de mapeamento).
    ev = _event(
        message={
            "chatid": "554499999999@c.us",
            "messageType": "extendedTextMessage",
            "content": "via content",
        }
    )
    canonical = normalize_uazapi_to_canonical(ev)
    assert canonical is not None
    assert canonical["phone"] == "554499999999"  # @c.us também é stripado
    assert canonical["text"] == {"message": "via content"}


# =========================================================================== #
# V1.2 — áudio (audio/ptt) e imagem
# =========================================================================== #
def test_v1_2_audio_messagetype_audio() -> None:
    ev = _event(
        message={
            "chatid": "554499999999@s.whatsapp.net",
            "messageType": "audioMessage",
            "fileURL": "https://uazapi.example/audio.ogg",
        }
    )
    canonical = normalize_uazapi_to_canonical(ev)
    assert canonical is not None
    assert canonical["audio"] == {"audioUrl": "https://uazapi.example/audio.ogg"}
    assert canonical["text"] is None
    assert canonical["image"] is None


def test_v1_2_audio_ptt_voice_discriminator() -> None:
    # Voz (push-to-talk): messageType == "ptt" NÃO contém "audio", mas DEVE
    # ser reconhecido como áudio (discriminador ptt — §3.5).
    ev = _event(
        message={
            "chatid": "554499999999@s.whatsapp.net",
            "messageType": "ptt",
            "fileURL": "https://uazapi.example/voice.ogg",
        }
    )
    canonical = normalize_uazapi_to_canonical(ev)
    assert canonical is not None
    assert canonical["audio"] == {"audioUrl": "https://uazapi.example/voice.ogg"}


def test_v1_2_audio_via_mediaurl_fallback() -> None:
    ev = _event(
        message={
            "chatid": "554499999999@s.whatsapp.net",
            "messageType": "audioMessage",
            "mediaUrl": "https://uazapi.example/m.ogg",
        }
    )
    canonical = normalize_uazapi_to_canonical(ev)
    assert canonical is not None
    assert canonical["audio"] == {"audioUrl": "https://uazapi.example/m.ogg"}


def test_v1_2_image_with_caption() -> None:
    ev = _event(
        message={
            "chatid": "554499999999@s.whatsapp.net",
            "messageType": "imageMessage",
            "fileURL": "https://uazapi.example/img.jpg",
            "caption": "olha isso",
        }
    )
    canonical = normalize_uazapi_to_canonical(ev)
    assert canonical is not None
    assert canonical["image"] == {
        "imageUrl": "https://uazapi.example/img.jpg",
        "caption": "olha isso",
    }
    assert canonical["text"] is None
    assert canonical["audio"] is None


# =========================================================================== #
# V1.3 — fromMe em todas as posições conhecidas (anti-loop)
# =========================================================================== #
def test_v1_3_from_me_top_level() -> None:
    ev = _event(
        message={
            "chatid": "554499999999@s.whatsapp.net",
            "messageType": "conversation",
            "text": "eco",
            "fromMe": True,
        }
    )
    canonical = normalize_uazapi_to_canonical(ev)
    assert canonical is not None
    assert canonical["fromMe"] is True


def test_v1_3_from_me_nested_in_key() -> None:
    # fromMe aninhado em message.key.fromMe (estilo Baileys).
    ev = _event(
        message={
            "chatid": "554499999999@s.whatsapp.net",
            "messageType": "conversation",
            "text": "eco",
            "key": {"fromMe": True, "id": "K-1"},
        }
    )
    canonical = normalize_uazapi_to_canonical(ev)
    assert canonical is not None
    assert canonical["fromMe"] is True


def test_v1_3_from_me_via_was_sent_by_api_flags() -> None:
    for flag in ("wasSentByApi", "fromApi", "sentByApi"):
        ev = _event(
            message={
                "chatid": "554499999999@s.whatsapp.net",
                "messageType": "conversation",
                "text": "eco",
                flag: True,
            }
        )
        canonical = normalize_uazapi_to_canonical(ev)
        assert canonical is not None, flag
        assert canonical["fromMe"] is True, flag


def test_v1_3_from_me_false_by_default() -> None:
    ev = _event(
        message={
            "chatid": "554499999999@s.whatsapp.net",
            "messageType": "conversation",
            "text": "msg de cliente",
        }
    )
    canonical = normalize_uazapi_to_canonical(ev)
    assert canonical is not None
    assert canonical["fromMe"] is False


# =========================================================================== #
# V1.4 — grupo: isGroup + phone do remetente (não do JID do grupo)
# =========================================================================== #
def test_v1_4_group_via_g_us_suffix_phone_from_participant() -> None:
    ev = _event(
        message={
            "chatid": "120363000000000000@g.us",
            "participant": "554488888888@s.whatsapp.net",
            "messageType": "conversation",
            "text": "no grupo",
        }
    )
    canonical = normalize_uazapi_to_canonical(ev)
    assert canonical is not None
    assert canonical["isGroup"] is True
    # phone deriva do REMETENTE (participant), não do JID do grupo
    assert canonical["phone"] == "554488888888"


def test_v1_4_group_via_isgroup_flag_phone_from_sender() -> None:
    ev = _event(
        message={
            "chatid": "120363000000000000@g.us",
            "sender": "554477777777@s.whatsapp.net",
            "isGroup": True,
            "messageType": "conversation",
            "text": "no grupo",
        }
    )
    canonical = normalize_uazapi_to_canonical(ev)
    assert canonical is not None
    assert canonical["isGroup"] is True
    assert canonical["phone"] == "554477777777"


def test_v1_4_non_group_is_false() -> None:
    ev = _event(
        message={
            "chatid": "554499999999@s.whatsapp.net",
            "messageType": "conversation",
            "text": "1:1",
        }
    )
    canonical = normalize_uazapi_to_canonical(ev)
    assert canonical is not None
    assert canonical["isGroup"] is False


# =========================================================================== #
# V1.5 — cascatas: connectedPhone / messageId / senderName
# =========================================================================== #
def test_v1_5_connected_phone_cascade_owner() -> None:
    ev = _event(
        message={
            "chatid": "554499999999@s.whatsapp.net",
            "messageType": "conversation",
            "text": "x",
        },
        connectedPhone=None,
        owner="5511888888888",
    )
    canonical = normalize_uazapi_to_canonical(ev)
    assert canonical is not None
    assert canonical["connectedPhone"] == "5511888888888"


def test_v1_5_connected_phone_cascade_instance_name() -> None:
    ev = _event(
        message={
            "chatid": "554499999999@s.whatsapp.net",
            "messageType": "conversation",
            "text": "x",
        },
        connectedPhone=None,
        owner=None,
        instanceName="5511777777777",
    )
    canonical = normalize_uazapi_to_canonical(ev)
    assert canonical is not None
    assert canonical["connectedPhone"] == "5511777777777"


def test_v1_5_message_id_prefers_messageid() -> None:
    ev = _event(
        message={
            "chatid": "554499999999@s.whatsapp.net",
            "messageType": "conversation",
            "text": "x",
            "messageid": "STABLE",
            "key": {"id": "FROM-KEY"},
            "id": "GENERIC",
        }
    )
    canonical = normalize_uazapi_to_canonical(ev)
    assert canonical is not None
    assert canonical["messageId"] == "STABLE"


def test_v1_5_message_id_falls_back_to_key_id() -> None:
    ev = _event(
        message={
            "chatid": "554499999999@s.whatsapp.net",
            "messageType": "conversation",
            "text": "x",
            "key": {"id": "FROM-KEY"},
            "id": "GENERIC",
        }
    )
    canonical = normalize_uazapi_to_canonical(ev)
    assert canonical is not None
    assert canonical["messageId"] == "FROM-KEY"


def test_v1_5_message_id_falls_back_to_generic_id() -> None:
    ev = _event(
        message={
            "chatid": "554499999999@s.whatsapp.net",
            "messageType": "conversation",
            "text": "x",
            "id": "GENERIC",
        }
    )
    canonical = normalize_uazapi_to_canonical(ev)
    assert canonical is not None
    assert canonical["messageId"] == "GENERIC"


def test_v1_5_sender_name_cascade() -> None:
    ev = _event(
        message={
            "chatid": "554499999999@s.whatsapp.net",
            "messageType": "conversation",
            "text": "x",
            "senderName": "Cliente A",
        }
    )
    canonical = normalize_uazapi_to_canonical(ev)
    assert canonical is not None
    assert canonical["senderName"] == "Cliente A"

    ev2 = _event(
        message={
            "chatid": "554499999999@s.whatsapp.net",
            "messageType": "conversation",
            "text": "x",
            "pushName": "Cliente B",
        }
    )
    canonical2 = normalize_uazapi_to_canonical(ev2)
    assert canonical2 is not None
    assert canonical2["senderName"] == "Cliente B"


# =========================================================================== #
# V1.6 — gate de event-type
# =========================================================================== #
def test_v1_6_non_inbound_events_return_none() -> None:
    # mesmo COM um objeto `message`, eventos de update/presença/etc -> None.
    msg = {
        "chatid": "554499999999@s.whatsapp.net",
        "messageType": "conversation",
        "text": "x",
    }
    for etype in (
        "messages_update",
        "presence",
        "connection",
        "contacts",
        "chats",
    ):
        ev = _event(message=msg, event=etype)
        assert normalize_uazapi_to_canonical(ev) is None, etype


def test_v1_6_messages_and_message_singular_accepted_case_insensitive() -> None:
    msg = {
        "chatid": "554499999999@s.whatsapp.net",
        "messageType": "conversation",
        "text": "x",
    }
    for etype in ("messages", "message", "MESSAGES", "Message"):
        ev = _event(message=msg, event=etype)
        canonical = normalize_uazapi_to_canonical(ev)
        assert canonical is not None, etype
        assert canonical["text"] == {"message": "x"}


def test_v1_6_event_via_eventtype_field() -> None:
    # EventType tem precedência sobre event (event-type cascade).
    msg = {
        "chatid": "554499999999@s.whatsapp.net",
        "messageType": "conversation",
        "text": "x",
    }
    ev_block = _event(message=msg, event="messages", EventType="presence")
    assert normalize_uazapi_to_canonical(ev_block) is None

    ev_ok = _event(message=msg, event="presence", EventType="messages")
    assert normalize_uazapi_to_canonical(ev_ok) is not None


# =========================================================================== #
# V1.7 — sem `message` -> None
# =========================================================================== #
def test_v1_7_no_message_returns_none() -> None:
    ev = _event(message=None)
    assert normalize_uazapi_to_canonical(ev) is None


# =========================================================================== #
# V1.8 — paridade de shape + _provider
# =========================================================================== #
def test_v1_8_shape_parity_and_provider_key() -> None:
    ev = _event(
        message={
            "chatid": "554499999999@s.whatsapp.net",
            "messageType": "conversation",
            "text": "x",
            "messageid": "M-1",
            "senderName": "Cliente",
            "messageTimestamp": 1700000000,
        }
    )
    canonical = normalize_uazapi_to_canonical(ev)
    assert canonical is not None

    # _provider presente e correto
    assert canonical["_provider"] == "uazapi"

    # o dict (sem _provider) tem exatamente as chaves consumidas por
    # ZAPIWebhookPayload / process_inbound.
    expected_keys = {
        "connectedPhone",
        "phone",
        "isGroup",
        "fromMe",
        "text",
        "audio",
        "image",
        "messageId",
        "senderName",
        "momment",
    }
    validatable = {k: v for k, v in canonical.items() if k != "_provider"}
    assert set(validatable.keys()) == expected_keys

    # e valida limpo no contrato canônico
    payload = ZAPIWebhookPayload(**validatable)
    assert payload.connectedPhone == "5511999999999"
    assert payload.messageId == "M-1"
    assert payload.momment == 1700000000


def test_v1_8_no_content_yields_dict_without_content() -> None:
    # tipo desconhecido sem corpo: retorna o dict (handler filtra no_content).
    ev = _event(
        message={
            "chatid": "554499999999@s.whatsapp.net",
            "messageType": "stickerMessage",
        }
    )
    canonical = normalize_uazapi_to_canonical(ev)
    assert canonical is not None
    assert canonical["text"] is None
    assert canonical["audio"] is None
    assert canonical["image"] is None
    assert canonical["_provider"] == "uazapi"


def test_v1_8_extra_fields_ignored_by_models() -> None:
    # extra: ignore — campos não mapeados não quebram a validação do model.
    ev = UazapiWebhookEvent(
        event="messages",
        connectedPhone="5511999999999",
        unknownEnvelopeField="whatever",
        message={
            "chatid": "554499999999@s.whatsapp.net",
            "messageType": "conversation",
            "text": "x",
            "someUnknownField": 123,
            "key": {"fromMe": False, "id": "K", "unknownKeyField": "z"},
        },
    )
    canonical = normalize_uazapi_to_canonical(ev)
    assert canonical is not None
    assert canonical["text"] == {"message": "x"}


# =========================================================================== #
# Allowlist union (§4.4) — generalização de _validate_inbound_media_url
# =========================================================================== #
def test_validate_inbound_media_url_unions_allowlists() -> None:
    # Defesa-em-profundidade: a allowlist usada é a UNIÃO zapi + uazapi +
    # evolution (§4.4 + bridge Evolution v2). Sem rede: o teste prova a UNIÃO via
    # grep do fonte (a validação SSRF real é coberta pela suíte SSRF). Robusto a
    # formatação: normaliza whitespace antes do match.
    import inspect

    src = inspect.getsource(wts._validate_inbound_media_url)
    normalized = " ".join(src.split())
    assert "settings.zapi_media_host_allowlist" in normalized
    assert "settings.uazapi_media_host_allowlist" in normalized
    assert "settings.evolution_media_host_allowlist" in normalized
    # as três somadas na mesma expressão de allowlist (união)
    assert (
        "settings.zapi_media_host_allowlist + settings.uazapi_media_host_allowlist"
        " + settings.evolution_media_host_allowlist" in normalized
    )
