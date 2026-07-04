"""
Conftest dos testes de core.

Semeia variáveis de ambiente mínimas ANTES de qualquer import de `app`, já que
`app.core.config.Settings()` é instanciado em import time e exige um conjunto de
variáveis obrigatórias. Os valores são dummies — nenhum serviço externo é
acessado nestes testes. Mesmo padrão de tests/services/conftest.py.
"""

from __future__ import annotations

import os
import pathlib
import sys

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _ensure_backend_on_path() -> None:
    backend = str(_BACKEND_ROOT)
    if backend not in sys.path:
        sys.path.insert(0, backend)


def _seed_env() -> None:
    defaults = {
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_KEY": "eyTest.eyTest.eyTest",
        "OPENAI_API_KEY": "sk-test",
        "MINIO_ROOT_USER": "minio",
        "MINIO_ROOT_PASSWORD": "minio123",
        "INTERNAL_JWT_SECRET": "0" * 64,
        "ENCRYPTION_KEY": "SlEoEOtyl89plqTxYcRD-B_9hIBad3-n1rr9isP442Y=",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)


_ensure_backend_on_path()
_seed_env()
