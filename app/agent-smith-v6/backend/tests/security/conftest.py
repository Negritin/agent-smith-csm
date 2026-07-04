"""
Conftest da suíte de segurança (isolamento multi-tenant — invariante nº 1).

Espelha tests/services/conftest.py:

1. Insere a raiz do backend no sys.path para `import app.*` resolver quando o
   pytest é invocado apontando direto para este diretório.
2. Semeia variáveis de ambiente mínimas exigidas por
   `app.core.config.Settings()` em import time. Valores são dummies — supabase,
   redis e o transporte MCP são SEMPRE fakes injetados; nenhum teste desta
   suíte toca rede ou banco.

O ambiente não possui pytest-asyncio; corrotinas via asyncio.run() (padrão do
projeto). Cenários concorrentes usam asyncio.gather DENTRO de uma corrotina
executada por asyncio.run.
"""

from __future__ import annotations

import os
import pathlib
import sys
import types

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _ensure_backend_on_path() -> None:
    backend = str(_BACKEND_ROOT)
    if backend not in sys.path:
        sys.path.insert(0, backend)


def _seed_env() -> None:
    defaults = {
        "SUPABASE_URL": "https://test.supabase.co",
        # JWT-shaped dummy: supabase-py valida a key contra um regex de JWT na
        # construção do client (sem rede).
        "SUPABASE_KEY": "eyTest.eyTest.eyTest",
        "OPENAI_API_KEY": "sk-test",
        "MINIO_ROOT_USER": "minio",
        "MINIO_ROOT_PASSWORD": "minio123",
        "INTERNAL_JWT_SECRET": "0" * 64,
        # Fernet key válida (satisfaz o EncryptionService no import).
        "ENCRYPTION_KEY": "SlEoEOtyl89plqTxYcRD-B_9hIBad3-n1rr9isP442Y=",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)


def _install_api_package() -> None:
    """Permite `import app.api.mcp` sem executar app/api/__init__.py.

    O __init__ do pacote api importa TODOS os routers (chat -> AudioService),
    que na suíte completa colidem com os pacotes sintéticos de app.services
    registrados pelos conftests de tests/agents. Mesmo padrão desses conftests:
    pacote sintético com __path__ REAL — o submódulo app.api.mcp resolve do
    disco; via setdefault, nunca sombreia um app.api já importado de verdade.
    """
    import app  # noqa: F401  (pacote real e leve)

    if "app.api" not in sys.modules:
        package = types.ModuleType("app.api")
        package.__path__ = [str(_BACKEND_ROOT / "app" / "api")]
        package.__package__ = "app.api"
        sys.modules["app.api"] = package
        # Atributo no pacote pai: monkeypatch resolve "app.api.mcp.<attr>"
        # via getattr a partir de `app` (como o import normal faria).
        setattr(app, "api", package)


_ensure_backend_on_path()
_seed_env()
_install_api_package()
