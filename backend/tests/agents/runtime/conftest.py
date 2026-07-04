"""
Conftest dos testes do Tool Runtime (registry/discovery/execução).

Espelha tests/services/conftest.py e tests/api/conftest.py:

1. Insere a raiz do backend no sys.path para `import app.*` resolver quando o
   pytest é invocado apontando direto para este diretório.
2. Semeia as variáveis de ambiente mínimas exigidas por
   `app.core.config.Settings()` em import time (instanciado eagerly ao
   importar `app.*`). Valores são dummies — os testes injetam FakeSupabase
   via client_provider e nunca tocam rede/banco.

Sem este conftest, rodar `pytest tests/agents/runtime/...` de forma isolada
falha na coleta (Settings exige SUPABASE_URL etc.); rodando `tests/agents`
inteiro, o conftest de tests/agents/graph stubava o ambiente por ordem
alfabética de coleta — efeito colateral, não contrato.

O ambiente não possui pytest-asyncio; corrotinas via asyncio.run() (padrão do
projeto).
"""

from __future__ import annotations

import os
import pathlib
import sys

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[3]


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


_ensure_backend_on_path()
_seed_env()
