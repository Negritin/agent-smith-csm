"""Fase 4b (PR-4 parte 2) — wiring do buffer_processor no seam (C1).

Protege a inversão de camadas concluída nesta fase:

  1. ESTRUTURAL: ``app.tasks.buffer_processor`` NÃO pode voltar a importar
     ``app.api.webhook`` (o turno WhatsApp mora em
     ``app.services.whatsapp_turn_service.process_inbound``). A verificação é
     por AST do FONTE em disco — sem importar o módulo, robusta a stubs de
     ``sys.modules`` de suites vizinhas.
  2. O import de ``process_inbound`` está no TOPO do módulo (module-level),
     não dentro de função (o import local era o sintoma da dependência da
     camada API).
  3. FAIL-FAST: ``start_buffer_scheduler`` exige o ``AsyncSupabaseClient``
     real na INICIALIZAÇÃO — chamada sem client (None) levanta ``RuntimeError``
     com mensagem clara, nunca adiando o erro para ``check_buffers`` (cujo
     catch-all o engoliria a cada 1s).

Convenções: env mínima semeada ANTES de importar app.* (Settings é construído
em import time); sem pytest-asyncio.
"""

from __future__ import annotations

import ast
import os
import pathlib

# --------------------------------------------------------------------------- #
# Env mínima ANTES de importar app.* (espelha test_lifespan_scheduler_gate.py).
# --------------------------------------------------------------------------- #
for _key, _value in {
    "SUPABASE_URL": "https://test.supabase.co",
    "SUPABASE_KEY": "eyTest.eyTest.eyTest",
    "OPENAI_API_KEY": "sk-test",
    "MINIO_ROOT_USER": "minio",
    "MINIO_ROOT_PASSWORD": "minio123",
    "INTERNAL_JWT_SECRET": "0" * 64,
    "ENCRYPTION_KEY": "SlEoEOtyl89plqTxYcRD-B_9hIBad3-n1rr9isP442Y=",
}.items():
    os.environ.setdefault(_key, _value)

import pytest  # noqa: E402

_BUFFER_PROCESSOR_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "app"
    / "tasks"
    / "buffer_processor.py"
)


def _module_imports() -> list[tuple[str, bool]]:
    """Lista (modulo_importado, é_top_level) de todo import do buffer_processor."""
    tree = ast.parse(_BUFFER_PROCESSOR_PATH.read_text(encoding="utf-8"))
    top_level_nodes = set(id(node) for node in tree.body)

    imports: list[tuple[str, bool]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((alias.name, id(node) in top_level_nodes))
        elif isinstance(node, ast.ImportFrom):
            imports.append((node.module or "", id(node) in top_level_nodes))
    return imports


# =========================================================================== #
# 1. Estrutural — buffer_processor NUNCA importa app.api.webhook (nem app.api.*)
# =========================================================================== #
def test_buffer_processor_does_not_import_webhook_router() -> None:
    offenders = [
        mod for mod, _top in _module_imports() if mod.startswith("app.api")
    ]
    assert offenders == [], (
        "app.tasks.buffer_processor must not depend on the API layer "
        f"(found imports: {offenders})"
    )


# =========================================================================== #
# 2. Import do service no TOPO do módulo (não local/lazy)
# =========================================================================== #
def test_process_inbound_imported_at_module_top_level() -> None:
    service_imports = [
        (mod, top)
        for mod, top in _module_imports()
        if mod == "app.services.whatsapp_turn_service"
    ]
    assert service_imports, "buffer_processor must import whatsapp_turn_service"
    assert all(top for _mod, top in service_imports), (
        "the whatsapp_turn_service import must be at module top level, "
        "not a local (lazy) import"
    )


# =========================================================================== #
# 3. Fail-fast — scheduler sem client async válido falha na INICIALIZAÇÃO
# =========================================================================== #
def test_start_buffer_scheduler_without_client_fails_fast() -> None:
    from app.tasks import buffer_processor

    with pytest.raises(RuntimeError) as exc:
        buffer_processor.start_buffer_scheduler(None)

    # Mensagem clara apontando a causa e a correção (client do lifespan).
    msg = str(exc.value)
    assert "AsyncSupabaseClient" in msg
    assert "supabase_async" in msg
    # E o scheduler NÃO foi iniciado.
    assert buffer_processor.scheduler.running is False


def test_start_buffer_scheduler_requires_positional_client() -> None:
    # A assinatura nova NÃO tem default: chamar sem argumento é TypeError
    # (regressão à assinatura antiga start_buffer_scheduler() deve falhar).
    from app.tasks import buffer_processor

    with pytest.raises(TypeError):
        buffer_processor.start_buffer_scheduler()  # type: ignore[call-arg]
