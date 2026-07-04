"""
Conftest dos testes de serviços (sprint "UCP Invalidation e Testes Finais").

Estes testes exercitam a camada de serviço REAL (app.services.ucp_service) para
provar que `ToolRegistry.invalidate` é chamado após cada mutação em
ucp_connections. Para isso:

1. Semeia variáveis de ambiente mínimas ANTES de qualquer import de `app`, já que
   `app.core.config.Settings()` é instanciado em import time e exige um conjunto
   de variáveis obrigatórias. Os valores são dummies — nenhum serviço externo é
   acessado nos testes (supabase, discovery e ToolRegistry são fakes injetados).

O ambiente não possui pytest-asyncio; seguimos o padrão dos demais testes do
projeto e usamos asyncio.run() para exercitar os métodos assíncronos.

2. (Fase 4b) Restaura os pacotes REAIS `app.services` / `app.core.database` /
   `app.api` em sys.modules antes do primeiro teste desta suíte rodar. Suítes
   vizinhas coletadas antes (tests/agents/tools, tests/security, tests/api)
   instalam pacotes sintéticos/stubs em sys.modules em tempo de COLEÇÃO; esses
   sintéticos têm `__path__` real (submódulos resolvem), mas o `__init__.py`
   nunca executa — então imports lazy como `from app.api.chat import
   ChatResponse` (json_renderer) quebram com "cannot import name ... (unknown
   location)". A fixture roda em tempo de EXECUÇÃO (depois que os golden tests
   de tests/agents já terminaram), purga apenas módulos sintéticos
   (`__spec__ is None`) e reimporta os reais do disco.
"""

from __future__ import annotations

import importlib
import os
import pathlib
import sys

import pytest

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _ensure_backend_on_path() -> None:
    backend = str(_BACKEND_ROOT)
    if backend not in sys.path:
        sys.path.insert(0, backend)


def _seed_env() -> None:
    defaults = {
        "SUPABASE_URL": "https://test.supabase.co",
        # JWT-shaped dummy: supabase-py valida a key contra um regex de JWT na
        # construção do client (sem rede). Módulos que constroem um client em
        # import time (ex.: app.api.webhook) exigem um dummy estruturalmente
        # válido aqui.
        "SUPABASE_KEY": "eyTest.eyTest.eyTest",
        "OPENAI_API_KEY": "sk-test",
        "MINIO_ROOT_USER": "minio",
        "MINIO_ROOT_PASSWORD": "minio123",
        "INTERNAL_JWT_SECRET": "0" * 64,
        # Fernet key válida (apenas para satisfazer o EncryptionService no import).
        "ENCRYPTION_KEY": "SlEoEOtyl89plqTxYcRD-B_9hIBad3-n1rr9isP442Y=",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)


_ensure_backend_on_path()
_seed_env()


# --------------------------------------------------------------------------- #
# Despoluição de sys.modules (Fase 4b) — só age sobre módulos SINTÉTICOS.
# --------------------------------------------------------------------------- #
# Ordem importa: database antes de services (o __init__ de app.api.chat importa
# de ambos), e api por último (executa `from .chat import router`).
_SYNTHETIC_CANDIDATES = (
    "app.services.search_service",
    "app.services.qdrant_service",
    "app.services.tavily_service",
    "app.services.filesystem_search_service",
    "app.core.database",
    "app.services",
    "app.api",
)


def _is_synthetic(module: object) -> bool:
    """Pacotes criados via types.ModuleType não têm __spec__ (nem loader)."""
    return getattr(module, "__spec__", None) is None


def _purge_synthetic_app_modules() -> None:
    for name in _SYNTHETIC_CANDIDATES:
        module = sys.modules.get(name)
        if module is not None and _is_synthetic(module):
            del sys.modules[name]


@pytest.fixture(scope="session", autouse=True)
def _restore_real_app_packages() -> None:
    """Garante que os pacotes reais estejam em sys.modules antes desta suíte.

    Em runs isolados (sem stubs) é um no-op: nada é sintético e os imports
    abaixo só retornam os módulos já cacheados.
    """
    _purge_synthetic_app_modules()
    importlib.import_module("app.core.database")
    importlib.import_module("app.services")
    importlib.import_module("app.api")
