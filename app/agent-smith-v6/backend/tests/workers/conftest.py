"""Conftest for worker (Celery billing) tests.

Mirrors tests/services/conftest.py:

1. Inserts the backend root on sys.path so ``import app.*`` resolves when pytest
   is invoked from elsewhere.
2. Seeds the minimal env vars that ``app.core.config.Settings()`` requires at
   import time (it is constructed eagerly when ``app`` modules are imported).
   All values are dummies — these tests inject fake supabase/billing stubs and
   never touch Redis, Supabase or the network.

No pytest-asyncio: the billing worker tasks are plain synchronous functions, so
tests call them directly with injected stubs.
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
