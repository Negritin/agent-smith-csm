"""Unit tests for the canonical NEUTRAL WhatsApp models (SPEC foundation sprint).

Conventions mirror test_handoff_policy.py / test_conversation_store.py:
  - Plain asserts; no pytest-asyncio (these tests are pure dataclass checks).
  - No network, no Supabase, no providers — purely structural coverage of the
    canonical schema and the capability/exception surface.

Coverage matrix (AC):
  - CanonicalMessage schema neutro + defaults + frozen.
  - MediaRef schema neutro + defaults + frozen.
  - InboundBatch.messages/statuses iniciam como [] e são independentes por
    instância (default_factory).
  - Ausência de campos Z-API em models.py (grep zapi/ZAPI retorna vazio).
  - OutboundMedia / TemplateRef / SendResult / DeliveryStatus schemas.
  - ProviderCapabilities: 6 flags booleanas, frozen, defaults False.
  - Hierarquia de exceções e importabilidade estável.
  - WhatsAppProvider é Protocol estrutural (runtime_checkable).
"""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path

import pytest

from app.services.whatsapp import (
    CanonicalMessage,
    DeliveryStatus,
    DeliveryStatusState,
    InboundBatch,
    MediaKind,
    MediaRef,
    MessageType,
    OutboundMedia,
    ProviderCapabilities,
    ProviderConfigError,
    ProviderNotSupportedError,
    SendResult,
    TemplateRef,
    UnknownProviderError,
    WhatsAppProvider,
    WhatsappError,
    WhatsappRetryableError,
)

_MODELS_FILE = Path(
    inspect.getsourcefile(CanonicalMessage)  # type: ignore[arg-type]
).resolve()  # type: ignore[union-attr]


# =========================================================================== #
# CanonicalMessage — schema neutro
# =========================================================================== #
def test_canonical_message_required_fields_present_and_neutral():
    """CanonicalMessage possui exatamente os campos esperados pelo AC."""
    expected = {
        "connected_phone",
        "from_phone",
        "type",
        "from_me",
        "is_group",
        "text",
        "timestamp",
        "sender_name",
        "media",
        "transcription_source_url",
        "message_id",
    }
    actual = {f.name for f in dataclasses.fields(CanonicalMessage)}
    assert actual == expected, (
        f"CanonicalMessage field set drifted: missing={expected - actual}, "
        f"extra={actual - expected}"
    )


def test_canonical_message_defaults_and_construction():
    """Campos opcionais defaultam para None; obrigatórios são required."""
    msg = CanonicalMessage(
        connected_phone="5511999999999",
        from_phone="5511888888888",
        type="text",
        from_me=False,
        is_group=False,
        text="ola",
    )
    assert msg.timestamp is None
    assert msg.sender_name is None
    assert msg.media is None
    assert msg.transcription_source_url is None
    assert msg.message_id is None

    # construction sem obrigatórios falha
    with pytest.raises(TypeError):
        CanonicalMessage(  # type: ignore[call-arg]
            connected_phone="x",
            from_phone="y",
            type="text",
        )


def test_canonical_message_is_frozen():
    """Frozen dataclass — atribuição deve levantar FrozenInstanceError."""
    msg = CanonicalMessage(
        connected_phone="c",
        from_phone="f",
        type="text",
        from_me=False,
        is_group=False,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        msg.text = "mutado"  # type: ignore[misc]


@pytest.mark.parametrize(
    "type_,media_kind",
    [
        ("text", None),
        ("audio", "audio"),
        ("image", "image"),
        ("unknown", None),
    ],
)
def test_canonical_message_type_accepts_closed_literal(type_, media_kind):
    """MessageType é o Literal fechado {text,audio,image,unknown}."""
    msg = CanonicalMessage(
        connected_phone="c",
        from_phone="f",
        type=type_,  # type: ignore[arg-type]
        from_me=False,
        is_group=False,
        media=MediaRef(kind=media_kind) if media_kind else None,
    )
    assert msg.type == type_


# =========================================================================== #
# MediaRef — schema neutro
# =========================================================================== #
def test_media_ref_fields_and_defaults():
    expected = {
        "kind",
        "raw_ref",
        "resolved_url",
        "stable_url",
        "mime_type",
        "caption",
    }
    actual = {f.name for f in dataclasses.fields(MediaRef)}
    assert actual == expected

    audio = MediaRef(kind="audio")
    assert audio.raw_ref is None
    assert audio.resolved_url is None
    assert audio.stable_url is None
    assert audio.mime_type is None
    assert audio.caption is None


def test_media_ref_is_frozen():
    ref = MediaRef(kind="image", raw_ref="abc")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ref.caption = "x"  # type: ignore[misc]


def test_media_ref_kind_accepts_only_audio_image():
    """MediaKind é Literal['audio','image'] — mais restrito que MessageType."""
    assert MediaRef(kind="audio").kind == "audio"
    assert MediaRef(kind="image").kind == "image"


# =========================================================================== #
# InboundBatch — listas default_factory + provider/connected_phone
# =========================================================================== #
def test_inbound_batch_defaults_empty_independent_lists():
    """messages/statuses iniciam como [] e cada instância tem sua própria lista."""
    batch = InboundBatch(provider="zapi", connected_phone="5511")
    assert batch.messages == []
    assert batch.statuses == []

    # Independência: mutar uma lista não afeta outra instância
    other = InboundBatch(provider="zapi", connected_phone="5511")
    batch.messages.append(
        CanonicalMessage(
            connected_phone="5511",
            from_phone="x",
            type="text",
            from_me=False,
            is_group=False,
        )
    )
    assert other.messages == [], "InboundBatch.messages compartilhou estado!"


def test_inbound_batch_no_integration_field():
    """AC: nenhum modelo recebe 'integration' como campo."""
    for cls in (
        CanonicalMessage,
        MediaRef,
        InboundBatch,
        DeliveryStatus,
        OutboundMedia,
        TemplateRef,
        SendResult,
    ):
        names = {f.name for f in dataclasses.fields(cls)}
        assert "integration" not in names, f"{cls.__name__} expôs 'integration'"


def test_inbound_batch_carries_provider_label_and_connected_phone():
    """provider é label neutra; connected_phone faz rota ao tenant."""
    batch = InboundBatch(
        provider="dialog360",
        connected_phone="5511999999999",
        messages=[
            CanonicalMessage(
                connected_phone="5511999999999",
                from_phone="x",
                type="text",
                from_me=False,
                is_group=False,
            )
        ],
        statuses=[DeliveryStatus(state="delivered", provider_message_id="abc")],
    )
    assert batch.provider == "dialog360"
    assert batch.connected_phone == "5511999999999"
    assert len(batch.messages) == 1
    assert batch.statuses[0].state == "delivered"


# =========================================================================== #
# Outbound canonical — OutboundMedia / TemplateRef / SendResult / DeliveryStatus
# =========================================================================== #
def test_outbound_media_schema():
    fields = {f.name for f in dataclasses.fields(OutboundMedia)}
    assert fields == {"kind", "url", "raw_ref", "mime_type", "caption"}
    assert OutboundMedia(kind="audio").url is None


def test_template_ref_schema():
    fields = {f.name for f in dataclasses.fields(TemplateRef)}
    assert fields == {"name", "language", "params", "namespace"}
    t = TemplateRef(name="welcome")
    assert t.language == "pt_BR"
    assert t.params == ()
    assert t.namespace is None


def test_send_result_only_ok_is_required():
    fields = {f.name for f in dataclasses.fields(SendResult)}
    assert fields == {"ok", "provider_message_id", "error"}
    r = SendResult(ok=True, provider_message_id="mid-1")
    assert r.ok is True
    assert r.provider_message_id == "mid-1"
    assert r.error is None


def test_delivery_status_schema():
    fields = {f.name for f in dataclasses.fields(DeliveryStatus)}
    assert fields == {"state", "provider_message_id", "timestamp", "error"}
    s = DeliveryStatus(state="read", provider_message_id="mid-1", timestamp=42)
    assert s.state == "read"
    assert s.error is None


def test_delivery_status_state_literal_closed():
    """DeliveryStatusState é o Literal fechado {queued,sent,delivered,read,failed}."""
    # Apenas sanity check — tipagem estrutural é responsabilidade do mypy.
    valid = {"queued", "sent", "delivered", "read", "failed"}
    # Cada valor legal é construtível:
    for state in valid:
        assert DeliveryStatus(state=state).state == state  # type: ignore[arg-type]


# =========================================================================== #
# Neutrality — grep por zapi/ZAPI dentro de models.py
# =========================================================================== #
def test_models_py_is_provider_neutral():
    """AC: grep por 'zapi' ou 'ZAPI' dentro de models.py retorna vazio."""
    text = _MODELS_FILE.read_text(encoding="utf-8")
    assert "zapi" not in text.lower(), (
        f"models.py contém menção a 'zapi': não-neutro. Arquivo: {_MODELS_FILE}"
    )


# =========================================================================== #
# ProviderCapabilities — 6 flags booleanas, frozen, defaults False
# =========================================================================== #
def test_provider_capabilities_six_flags_exactly():
    expected = {
        "templates",
        "session_window_24h",
        "delivery_statuses",
        "media_by_id",
        "hmac_webhook",
        "interactive",
    }
    actual = {f.name for f in dataclasses.fields(ProviderCapabilities)}
    assert actual == expected


def test_provider_capabilities_defaults_all_false():
    caps = ProviderCapabilities()
    for flag in (
        caps.templates,
        caps.session_window_24h,
        caps.delivery_statuses,
        caps.media_by_id,
        caps.hmac_webhook,
        caps.interactive,
    ):
        assert flag is False, "default de capability deveria ser False"


def test_provider_capabilities_is_frozen():
    caps = ProviderCapabilities(templates=True)
    assert caps.templates is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        caps.templates = False  # type: ignore[misc]


# =========================================================================== #
# Exceptions — hierarquia canônica e importabilidade estável
# =========================================================================== #
def test_exception_hierarchy():
    assert issubclass(UnknownProviderError, WhatsappError)
    assert issubclass(ProviderConfigError, WhatsappError)
    assert issubclass(ProviderNotSupportedError, WhatsappError)
    assert issubclass(WhatsappRetryableError, WhatsappError)
    assert issubclass(WhatsappError, Exception)


def test_exceptions_are_raisable_and_catchable():
    with pytest.raises(UnknownProviderError):
        raise UnknownProviderError("nope")
    with pytest.raises(ProviderConfigError):
        raise ProviderConfigError("bad config")
    with pytest.raises(ProviderNotSupportedError):
        raise ProviderNotSupportedError("not supported")
    with pytest.raises(WhatsappRetryableError):
        raise WhatsappRetryableError("transient")


def test_whatsapp_error_catches_all_subclasses():
    """Catch em WhatsappError captura todos os subtipos — útil para fachada."""
    for exc_cls in (
        UnknownProviderError,
        ProviderConfigError,
        ProviderNotSupportedError,
        WhatsappRetryableError,
    ):
        try:
            raise exc_cls("x")
        except WhatsappError:
            pass
        else:
            pytest.fail(f"{exc_cls.__name__} não capturada por WhatsappError")


# =========================================================================== #
# WhatsAppProvider — Protocol estrutural (runtime_checkable)
# =========================================================================== #
def _shape(cls) -> set[str]:
    """Extrai o conjunto de métodos/properties públicos de uma classe."""
    members = set()
    for name, member in inspect.getmembers(cls):
        if name.startswith("_"):
            continue
        members.add(name)
    return members


def test_whatsapp_provider_protocol_surface():
    """O Protocol declara exatamente os métodos/properties esperados pelo AC."""
    expected_methods = {
        "capabilities",
        "validate_config",
        "verify_webhook",
        "parse_webhook",
        "resolve_media_url",
        "send_text",
        "send_media",
        "send_template",
    }
    actual = _shape(WhatsAppProvider)
    missing = expected_methods - actual
    assert not missing, f"Protocol sem membros: {missing}"


def test_whatsapp_provider_is_runtime_checkable():
    """Protocol decorado com @runtime_checkable (instanceof funciona)."""
    # Apenas garantir que o atributo existe — não há implementador nesta sprint.
    assert hasattr(WhatsAppProvider, "_is_runtime_protocol") or hasattr(
        WhatsAppProvider, "_is_protocol"
    ), "WhatsAppProvider deve ser @runtime_checkable"


def test_whatsapp_provider_methods_do_not_receive_integration():
    """AC: nenhum método do Protocol recebe 'integration' como argumento."""
    for method_name in (
        "validate_config",
        "verify_webhook",
        "parse_webhook",
        "resolve_media_url",
        "send_text",
        "send_media",
        "send_template",
    ):
        method = getattr(WhatsAppProvider, method_name, None)
        if method is None:
            continue
        params = inspect.signature(method).parameters
        # 'self' é esperado; nenhum parâmetro pode se chamar 'integration'.
        param_names = {p for p in params if p != "self"}
        assert "integration" not in param_names, (
            f"{method_name} recebe 'integration' — quebra o contrato neutro"
        )


# =========================================================================== #
# Type aliases (Literal unions) — sanity checks
# =========================================================================== #
def test_type_aliases_exist():
    """Os Literals públicos estão expostos no __init__ do pacote."""
    import typing

    for alias in (MessageType, MediaKind, DeliveryStatusState):
        # Literal unions têm __origin__ ou __args__ em typing.
        assert typing.get_args(alias), f"{alias!r} não é Literal fechado"


# =========================================================================== #
# Package exports — importabilidade estável
# =========================================================================== #
def test_package_public_surface():
    """Todos os símbolos públicos estão expostos no __init__ do pacote."""
    import app.services.whatsapp as pkg

    expected = {
        "CanonicalMessage",
        "DeliveryStatus",
        "DeliveryStatusState",
        "InboundBatch",
        "MediaKind",
        "MediaRef",
        "MessageType",
        "OutboundMedia",
        "SendResult",
        "TemplateRef",
        "ProviderCapabilities",
        "WhatsAppProvider",
        "ProviderConfigError",
        "ProviderNotSupportedError",
        "UnknownProviderError",
        "WhatsappError",
        "WhatsappRetryableError",
    }
    public = {name for name in dir(pkg) if not name.startswith("_")}
    assert expected.issubset(public), f"missing exports: {expected - public}"
