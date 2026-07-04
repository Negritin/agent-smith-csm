"""BAIXO-006 — address_country derivado do tenant, não mais constante.

Antes: o envelope UCP de ``search_catalog`` mandava ``address_country`` HARDCODED
em 'BR' (TODO multi-tenant aberto), gerando catálogo/preço errados p/ lojas fora
do Brasil. Agora ``StorefrontMCPClient.search_products`` recebe ``address_country``
(derivado do agente/loja) e só cai no fallback EXPLÍCITO 'BR' — com log de aviso —
quando o país não chega.

Convenção (espelha test_memory_service_shell.py): env dummy semeado ANTES de
importar app.*, ``asyncio.run`` para o async, plain asserts, sem rede real (o
``_call_mcp_tool`` é stubado e captura o envelope enviado).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict

for _k, _v in {
    "SUPABASE_URL": "https://test.supabase.co",
    "SUPABASE_KEY": "test-key",
    "OPENAI_API_KEY": "sk-test",
    "MINIO_ROOT_USER": "minio",
    "MINIO_ROOT_PASSWORD": "minio123",
    "INTERNAL_JWT_SECRET": "0" * 64,
    "ENCRYPTION_KEY": "SlEoEOtyl89plqTxYcRD-B_9hIBad3-n1rr9isP442Y=",
}.items():
    os.environ.setdefault(_k, _v)

from app.services import storefront_mcp as sm  # noqa: E402
from app.services.storefront_mcp import (  # noqa: E402
    DEFAULT_ADDRESS_COUNTRY,
    StorefrontMCPClient,
)


def _client_capturing(captured: Dict[str, Any]) -> StorefrontMCPClient:
    client = StorefrontMCPClient("loja.myshopify.com")

    async def _fake_call(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        captured["tool"] = tool_name
        captured["arguments"] = arguments
        return {"products": []}

    client._call_mcp_tool = _fake_call  # type: ignore[assignment]
    return client


def test_configured_country_is_forwarded_without_fallback_warning(caplog) -> None:
    captured: Dict[str, Any] = {}
    client = _client_capturing(captured)

    with caplog.at_level(logging.WARNING, logger=sm.logger.name):
        result = asyncio.run(client.search_products("vestido", address_country="US"))

    assert result.success is True
    assert captured["arguments"]["catalog"]["context"]["address_country"] == "US"
    # País veio da config -> NENHUM aviso de fallback.
    assert not [r for r in caplog.records if "fallback" in r.message]


def test_missing_country_falls_back_to_br_with_warning(caplog) -> None:
    captured: Dict[str, Any] = {}
    client = _client_capturing(captured)

    with caplog.at_level(logging.WARNING, logger=sm.logger.name):
        result = asyncio.run(client.search_products("vestido"))  # sem address_country

    assert result.success is True
    assert (
        captured["arguments"]["catalog"]["context"]["address_country"]
        == DEFAULT_ADDRESS_COUNTRY
        == "BR"
    )
    # Fallback EXPLÍCITO deve registrar aviso.
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("address_country não configurado" in m for m in warnings)


def test_no_hardcoded_country_literal_in_arguments() -> None:
    """Guarda contra regressão: o país não pode voltar a ser constante inline.

    O TODO + literal ``"address_country": "BR"`` foi removido; agora o valor vem
    de ``resolved_country`` (parâmetro/fallback), nunca de uma string literal no
    dicionário de argumentos.
    """
    import inspect

    src = inspect.getsource(StorefrontMCPClient.search_products)
    assert "TODO" not in src
    assert '"address_country": "BR"' not in src
    assert "resolved_country" in src
