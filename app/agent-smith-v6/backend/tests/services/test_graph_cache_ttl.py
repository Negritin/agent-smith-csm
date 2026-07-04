"""F11 (G2-R10) — the graph cache is a TTLCache with a finite TTL (defense in
depth for the horizontal-scale jump in F09).

Primary fix for the stale-graph bug is the BEFORE UPDATE trigger on public.agents
(updated_at advances -> cache KEY changes -> stale entry stops matching). The TTL
here is the guard-rail: even in a pathological scenario no graph entry stays cached
longer than ``ttl``. The swap must keep the dict-style API used by
``get_or_create_graph`` / ``invalidate_agent_graph_cache``.

What this proves:
  - an entry inserted into _graphs_cache EXPIRES after the TTL (fake clock:
    ``key in _graphs_cache`` flips to False once the timer passes ttl);
  - the module wires a TTLCache (not a plain/LRU cache) with the documented TTL;
  - ``invalidate_agent_graph_cache`` by company:agent prefix STILL removes entries
    (the in-process invalidation path keeps working).

Conventions: env seeded by tests/services/conftest.py before importing app.*; we
swap in a fresh TTLCache backed by a controllable fake clock so the REAL module
functions are exercised against a fake timer (the standard cachetools pattern).
"""

from __future__ import annotations

import pytest
from cachetools import TTLCache

import app.services.graph_cache as graph_cache


class _FakeClock:
    """Monotonic-style timer whose value only moves when we tick it."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def tick(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def fake_clock_cache(monkeypatch):
    """Replace _graphs_cache with a TTLCache driven by a fake clock.

    Uses the module's own GRAPH_CACHE_TTL_SECONDS so the test tracks the real TTL.
    """
    clock = _FakeClock()
    ttl = graph_cache.GRAPH_CACHE_TTL_SECONDS
    cache = TTLCache(maxsize=500, ttl=ttl, timer=clock)
    monkeypatch.setattr(graph_cache, "_graphs_cache", cache, raising=True)
    return clock, ttl


# --------------------------------------------------------------------------- #
# 1. O módulo usa TTLCache com TTL finito e positivo.
# --------------------------------------------------------------------------- #
def test_graph_cache_is_ttl_cache() -> None:
    assert isinstance(graph_cache._graphs_cache, TTLCache)
    assert graph_cache.GRAPH_CACHE_TTL_SECONDS > 0
    assert graph_cache._graphs_cache.ttl == graph_cache.GRAPH_CACHE_TTL_SECONDS


# --------------------------------------------------------------------------- #
# 2. Uma entrada expira após o TTL (clock fake).
# --------------------------------------------------------------------------- #
def test_entry_expires_after_ttl(fake_clock_cache) -> None:
    clock, ttl = fake_clock_cache
    key = "company-1:agent-1:deadbeef"
    graph_cache._graphs_cache[key] = object()

    # Antes do TTL: ainda presente.
    clock.tick(ttl - 1)
    assert key in graph_cache._graphs_cache

    # Depois do TTL: expirado.
    clock.tick(2)
    assert key not in graph_cache._graphs_cache


# --------------------------------------------------------------------------- #
# 3. invalidate_agent_graph_cache por prefixo continua removendo entradas.
# --------------------------------------------------------------------------- #
def test_invalidate_by_prefix_still_works(fake_clock_cache) -> None:
    _clock, _ttl = fake_clock_cache

    # Duas versões do mesmo (company, agent) + um agente não relacionado.
    graph_cache._graphs_cache["c1:a1:v1"] = object()
    graph_cache._graphs_cache["c1:a1:v2"] = object()
    graph_cache._graphs_cache["c1:a2:v1"] = object()

    graph_cache.invalidate_agent_graph_cache("c1", "a1")

    assert "c1:a1:v1" not in graph_cache._graphs_cache
    assert "c1:a1:v2" not in graph_cache._graphs_cache
    # O agente não relacionado permanece.
    assert "c1:a2:v1" in graph_cache._graphs_cache


# --------------------------------------------------------------------------- #
# 4. compute_graph_cache_key muda quando updated_at muda (chave invalida sozinha
#    quando o trigger do F11 avança updated_at).
# --------------------------------------------------------------------------- #
def test_cache_key_changes_on_updated_at() -> None:
    base = {
        "updated_at": "2026-06-01T00:00:00+00:00",
        "llm_provider": "openai",
        "llm_model": "gpt-4o",
        "tools": [],
        "delegations": [],
    }
    newer = dict(base, updated_at="2026-06-01T00:00:01+00:00")

    k1 = graph_cache.compute_graph_cache_key("c1", "a1", base)
    k2 = graph_cache.compute_graph_cache_key("c1", "a1", newer)
    assert k1 != k2
    # O prefixo company:agent é preservado (a invalidação por prefixo depende disso).
    assert k1.startswith("c1:a1:")
    assert k2.startswith("c1:a1:")
