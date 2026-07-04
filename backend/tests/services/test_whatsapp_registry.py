"""Testes do registry de providers WhatsApp.

SPEC — Sprint "Registry + estreitamento de WHATSAPP_PROVIDERS".

Cobre :func:`app.services.whatsapp.registry.resolve_provider`:

  - Normalização da string ``provider`` (``lower()`` + ``strip()``);
  - Alias ``evolution-api`` -> ``evolution``;
  - INSTÂNCIA NOVA a cada resolução (config amarrada na construção,
    isolamento multi-tenant) — nunca um singleton;
  - :class:`UnknownProviderError` (ZERO fallback para Z-API, SEC-04) para
    providers fora de ``{z-api, uazapi, evolution}`` (+alias), incluindo
    ``wppconnect``/``whatsapp``/``whatsapp-cloud``/``meta``.

Convenções: SEM pytest-asyncio (asserts sync); env semeado por
tests/services/conftest.py antes de importar app.*.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from app.services.whatsapp.exceptions import UnknownProviderError
from app.services.whatsapp.providers.evolution import EvolutionProvider
from app.services.whatsapp.providers.uazapi import UazapiProvider
from app.services.whatsapp.providers.zapi import ZapiProvider
from app.services.whatsapp.registry import resolve_provider


# --------------------------------------------------------------------------- #
# Integrations VÁLIDAS por provider (satisfazem validate_config de cada bridge)
# --------------------------------------------------------------------------- #
def _zapi_integration(**extra: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "provider": "z-api",
        "instance_id": "inst-zapi",
        "token": "tok-zapi",
    }
    base.update(extra)
    return base


def _uazapi_integration(**extra: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "provider": "uazapi",
        "base_url": "https://xxx.uazapi.com",
        "token": "tok-uazapi",
    }
    base.update(extra)
    return base


def _evolution_integration(**extra: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "provider": "evolution",
        "base_url": "https://evo.example.com",
        "instance_id": "inst-evo",
        "token": "tok-evo",
    }
    base.update(extra)
    return base


# =========================================================================== #
# Resolução canônica: cada provider mapeia para o bridge correto
# =========================================================================== #
def test_resolve_zapi_builds_zapi_provider() -> None:
    provider = resolve_provider(_zapi_integration())
    assert isinstance(provider, ZapiProvider)


def test_resolve_uazapi_builds_uazapi_provider() -> None:
    provider = resolve_provider(_uazapi_integration())
    assert isinstance(provider, UazapiProvider)


def test_resolve_evolution_builds_evolution_provider() -> None:
    provider = resolve_provider(_evolution_integration())
    assert isinstance(provider, EvolutionProvider)


# =========================================================================== #
# Normalização: lower() + strip()
# =========================================================================== #
@pytest.mark.parametrize(
    "raw_label",
    ["Z-API", "  z-api  ", "Z-Api\n", "\tz-api"],
)
def test_resolve_normalizes_case_and_whitespace(raw_label: str) -> None:
    provider = resolve_provider(_zapi_integration(provider=raw_label))
    assert isinstance(provider, ZapiProvider)


def test_resolve_uazapi_normalizes_uppercase() -> None:
    provider = resolve_provider(_uazapi_integration(provider="UAZAPI"))
    assert isinstance(provider, UazapiProvider)


# =========================================================================== #
# Alias: evolution-api -> evolution
# =========================================================================== #
def test_resolve_alias_evolution_api_maps_to_evolution() -> None:
    provider = resolve_provider(_evolution_integration(provider="evolution-api"))
    assert isinstance(provider, EvolutionProvider)


def test_resolve_alias_evolution_api_normalized_then_aliased() -> None:
    # casing/whitespace + alias aplicados juntos
    provider = resolve_provider(_evolution_integration(provider="  Evolution-API "))
    assert isinstance(provider, EvolutionProvider)


# =========================================================================== #
# Instância NOVA a cada resolução (multi-tenant, sem singleton)
# =========================================================================== #
def test_resolve_returns_new_instance_each_call() -> None:
    integ = _zapi_integration()
    a = resolve_provider(integ)
    b = resolve_provider(integ)
    assert a is not b


def test_resolve_distinct_tenants_isolated_instances() -> None:
    a = resolve_provider(_uazapi_integration(token="tok-A"))
    b = resolve_provider(_uazapi_integration(token="tok-B"))
    assert a is not b
    assert isinstance(a, UazapiProvider)
    assert isinstance(b, UazapiProvider)


# =========================================================================== #
# UnknownProviderError — ZERO fallback para Z-API (SEC-04)
# =========================================================================== #
@pytest.mark.parametrize(
    "unknown",
    ["wppconnect", "whatsapp", "whatsapp-cloud", "meta", "telegram", "sms"],
)
def test_resolve_unknown_provider_raises(unknown: str) -> None:
    with pytest.raises(UnknownProviderError):
        resolve_provider(_zapi_integration(provider=unknown))


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_resolve_empty_provider_raises(empty: Any) -> None:
    with pytest.raises(UnknownProviderError):
        resolve_provider(_zapi_integration(provider=empty))


def test_resolve_missing_provider_key_raises() -> None:
    with pytest.raises(UnknownProviderError):
        resolve_provider({"instance_id": "x", "token": "y"})


def test_unknown_provider_has_no_zapi_fallback() -> None:
    # Mesmo com config z-api VÁLIDA no dict, um label desconhecido NÃO cai em
    # Z-API: levanta UnknownProviderError em vez de construir ZapiProvider.
    integ = _zapi_integration(provider="meta")
    with pytest.raises(UnknownProviderError):
        resolve_provider(integ)
