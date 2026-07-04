"""
Sprint 1 — tests for the ChatTurnOrchestrator seam (SPEC 20260529_172113-738e71).

The seam is BUILT this sprint but NOT wired into any entrypoint. These tests
drive the orchestrator directly with injected fakes.

Conventions (mirror tests/services/test_chat_turn_characterization.py):
  - NO pytest-asyncio; async is driven with asyncio.run(...).
  - Plain asserts; fakes/stubs injected.
  - Env vars seeded by tests/services/conftest.py BEFORE importing app.*.
  - NO external service touched (Supabase, LLM, Qdrant, Groq all faked).

NOTE on the recovery tests: the REAL stream_agent/invoke_agent still swallow
errors (that's fixed in Sprint 3). Here we inject FAKE adapters that PROPAGATE
the error so _with_recovery can see it — exactly the contract Sprint 3 will make
the real adapters honor.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional

import app.services.chat_turn_orchestrator as cto
import app.services.graph_cache as gc
from app.services.chat_turn_orchestrator import (
    ChatTurnOrchestrator,
    StreamEvent,
    TurnRequest,
)


# =========================================================================== #
# Shared fakes
# =========================================================================== #
class _Result:
    def __init__(self, data: Any) -> None:
        self.data = data


class _AgentQuery:
    def __init__(self, store: "FakeSupabase", table: str) -> None:
        self._store = store
        self._table = table

    def select(self, *_a: Any, **_k: Any) -> "_AgentQuery":
        return self

    def eq(self, *_a: Any, **_k: Any) -> "_AgentQuery":
        return self

    def order(self, *_a: Any, **_k: Any) -> "_AgentQuery":
        return self

    def limit(self, *_a: Any, **_k: Any) -> "_AgentQuery":
        return self

    def execute(self) -> _Result:
        self._store.query_count += 1
        if self._table == "agents":
            return _Result([self._store.agent] if self._store.agent else [])
        return _Result([])


class _FakeClient:
    def __init__(self, store: "FakeSupabase") -> None:
        self._store = store

    def table(self, name: str) -> _AgentQuery:
        self._store.table_calls.append(name)
        return _AgentQuery(self._store, name)


class FakeSupabase:
    def __init__(
        self,
        company: Optional[Dict[str, Any]],
        agent: Optional[Dict[str, Any]],
        history: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        self.company = company
        self.agent = agent
        self.client = _FakeClient(self)
        self._history = history if history is not None else []
        self.query_count = 0
        self.get_company_count = 0
        self.history_count = 0
        self.table_calls: List[str] = []

    def get_company(self, company_id: str) -> Optional[Dict[str, Any]]:
        self.get_company_count += 1
        return self.company

    def get_conversation_history(self, **_k: Any) -> List[Dict[str, str]]:
        self.history_count += 1
        return self._history


class _RecordingGuardrail:
    """Fake guardrail that records the exact text validate_input received."""

    received: List[str] = []

    def __init__(self, agent_config: Dict[str, Any], company_id: str) -> None:
        self.fail_close = True

    async def validate_input(self, text: str):
        _RecordingGuardrail.received.append(text)
        return False, "", text


def _install_guardrail(monkeypatch_cls):
    """Patch SmithGuardrail where _run_guardrail imports it (local import from
    app.agents.guardrails). Returns a restore callable."""
    import app.agents.guardrails as gr

    orig = gr.SmithGuardrail
    gr.SmithGuardrail = monkeypatch_cls  # type: ignore[assignment]

    def _restore():
        gr.SmithGuardrail = orig  # type: ignore[assignment]

    return _restore


def _install_adapter(name: str, fake):
    """Patch invoke_agent / stream_agent on the live app.agents package
    (imported locally inside the orchestrator). Returns a restore callable."""
    import importlib

    pkg = importlib.import_module("app.agents")
    had = hasattr(pkg, name)
    orig = getattr(pkg, name, None)
    setattr(pkg, name, fake)

    def _restore():
        if had:
            setattr(pkg, name, orig)
        elif hasattr(pkg, name):
            delattr(pkg, name)

    return _restore


def _install_create_agent_graph(fake):
    """get_or_create_graph imports create_agent_graph locally from app.agents."""
    import importlib

    pkg = importlib.import_module("app.agents")
    had = hasattr(pkg, "create_agent_graph")
    orig = getattr(pkg, "create_agent_graph", None)
    pkg.create_agent_graph = fake  # type: ignore[assignment]

    def _restore():
        if had:
            pkg.create_agent_graph = orig  # type: ignore[assignment]
        elif hasattr(pkg, "create_agent_graph"):
            delattr(pkg, "create_agent_graph")

    return _restore


def _install_close_pool(fake):
    import app.agents.graph as graph_mod

    orig = graph_mod.close_async_postgres_pool
    graph_mod.close_async_postgres_pool = fake  # type: ignore[assignment]

    def _restore():
        graph_mod.close_async_postgres_pool = orig  # type: ignore[assignment]

    return _restore


def _make_agent(**overrides: Any) -> Dict[str, Any]:
    base = {
        "id": "agent-1",
        "llm_provider": "openai",
        "llm_model": "gpt-4o",
        "security_settings": {"enabled": True},
        "updated_at": "2026-05-29T00:00:00",
    }
    base.update(overrides)
    return base


def _orch(supabase) -> ChatTurnOrchestrator:
    return ChatTurnOrchestrator(
        supabase_client=supabase,
        qdrant_service=None,
        conversation_store=None,
        billing_gate=None,
        handoff_policy=None,
    )


def _stub_graph_acquire():
    """Make get_or_create_graph return a sentinel without touching create_agent_graph."""
    async def _fake_create(**_k: Any):
        return object()

    return _install_create_agent_graph(_fake_create)


def _drain_stream(orch, req) -> List[StreamEvent]:
    async def _run():
        out: List[StreamEvent] = []
        async for ev in orch.stream_turn(req):
            out.append(ev)
        return out

    return asyncio.run(_run())


# =========================================================================== #
# Import-cycle test
# =========================================================================== #
def test_no_import_cycle_any_order():
    import importlib

    for order in (
        ["app.services.chat_turn_orchestrator", "app.services.langchain_service", "app.services.graph_cache"],
        ["app.services.langchain_service", "app.services.graph_cache", "app.services.chat_turn_orchestrator"],
        ["app.services.graph_cache", "app.services.chat_turn_orchestrator", "app.services.langchain_service"],
    ):
        for mod in order:
            importlib.import_module(mod)  # must not raise ImportError


# =========================================================================== #
# Pipeline order (D1/D2)
# =========================================================================== #
def test_run_turn_guardrail_receives_enriched_with_image():
    _RecordingGuardrail.received = []
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent(vision_model="gpt-4o"))
    orch = _orch(supabase)

    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    captured: List[Dict[str, Any]] = []

    async def _fake_invoke(**kwargs: Any):
        captured.append(kwargs)
        return {"response": "ok", "tokens_total": 4}

    restore_inv = _install_adapter("invoke_agent", _fake_invoke)

    # Vision returns a deterministic description (patched on the orchestrator).
    orig_analyze = ChatTurnOrchestrator._analyze_image
    ChatTurnOrchestrator._analyze_image = staticmethod(  # type: ignore[assignment]
        lambda *a, **k: "DESC_FROM_IMAGE"
    )
    try:
        result = asyncio.run(
            orch.run_turn(
                TurnRequest(
                    user_message="hello",
                    company_id="c1",
                    session_id="s1",
                    user_id="u1",
                    agent_id="agent-1",
                    image_url="https://img/x.png",
                )
            )
        )
    finally:
        ChatTurnOrchestrator._analyze_image = orig_analyze  # type: ignore[assignment]
        restore_gr()
        restore_graph()
        restore_inv()

    assert result.response == "ok"
    assert result.tokens_total == 4
    # Guardrail saw the ENRICHED text (with [CONTEXTO VISUAL]).
    assert len(_RecordingGuardrail.received) == 1
    assert "[CONTEXTO VISUAL]" in _RecordingGuardrail.received[0]
    assert "DESC_FROM_IMAGE" in _RecordingGuardrail.received[0]
    # invoke got the same enriched message.
    assert "[CONTEXTO VISUAL]" in captured[0]["user_message"]


def test_run_turn_without_image_enriched_equals_sanitized():
    _RecordingGuardrail.received = []
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    orch = _orch(supabase)

    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    captured: List[Dict[str, Any]] = []

    async def _fake_invoke(**kwargs: Any):
        captured.append(kwargs)
        return {"response": "ok", "tokens_total": 1}

    restore_inv = _install_adapter("invoke_agent", _fake_invoke)
    try:
        asyncio.run(
            orch.run_turn(
                TurnRequest(
                    user_message="plain text",
                    company_id="c1",
                    session_id="s1",
                    agent_id="agent-1",
                )
            )
        )
    finally:
        restore_gr()
        restore_graph()
        restore_inv()

    assert _RecordingGuardrail.received == ["plain text"]
    assert captured[0]["user_message"] == "plain text"


def test_stream_turn_guardrail_receives_enriched_with_image():
    _RecordingGuardrail.received = []
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent(vision_model="gpt-4o"))
    orch = _orch(supabase)

    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    async def _fake_stream(**_kwargs: Any):
        for t in ["a", "b"]:
            yield t

    restore_stream = _install_adapter("stream_agent", _fake_stream)
    orig_analyze = ChatTurnOrchestrator._analyze_image
    ChatTurnOrchestrator._analyze_image = staticmethod(  # type: ignore[assignment]
        lambda *a, **k: "DESC_FROM_IMAGE"
    )
    try:
        events = _drain_stream(
            orch,
            TurnRequest(
                user_message="hello",
                company_id="c1",
                session_id="s1",
                agent_id="agent-1",
                image_url="https://img/x.png",
            ),
        )
    finally:
        ChatTurnOrchestrator._analyze_image = orig_analyze  # type: ignore[assignment]
        restore_gr()
        restore_graph()
        restore_stream()

    tokens = [e.data for e in events if e.type == "token"]
    assert tokens == ["a", "b"]
    assert events[-1].type == "done"
    assert len(_RecordingGuardrail.received) == 1
    assert "[CONTEXTO VISUAL]" in _RecordingGuardrail.received[0]


# =========================================================================== #
# api_key (D3)
# =========================================================================== #
def test_provider_precedence_agent_then_company_then_openai():
    _RecordingGuardrail.received = []
    os.environ["ANTHROPIC_API_KEY"] = "ak-test"
    os.environ["OPENAI_API_KEY"] = "sk-test"

    seen_provider: List[str] = []
    orig_resolver = cto.get_api_key_for_provider

    def _spy(provider=None, model=None):
        seen_provider.append(provider)
        return "resolved-key"

    cto.get_api_key_for_provider = _spy  # type: ignore[assignment]

    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    async def _fake_invoke(**_k: Any):
        return {"response": "ok", "tokens_total": 0}

    restore_inv = _install_adapter("invoke_agent", _fake_invoke)
    try:
        # agent.llm_provider wins
        supabase = FakeSupabase(
            company={"id": "c1", "llm_provider": "google"},
            agent=_make_agent(llm_provider="anthropic"),
        )
        asyncio.run(
            _orch(supabase).run_turn(
                TurnRequest(user_message="hi", company_id="c1", session_id="s1", agent_id="agent-1")
            )
        )
        assert seen_provider[-1] == "anthropic"

        # no agent provider -> company provider
        supabase = FakeSupabase(
            company={"id": "c1", "llm_provider": "google"},
            agent=_make_agent(llm_provider=None),
        )
        asyncio.run(
            _orch(supabase).run_turn(
                TurnRequest(user_message="hi", company_id="c1", session_id="s1", agent_id="agent-1")
            )
        )
        assert seen_provider[-1] == "google"

        # neither -> "openai"
        supabase = FakeSupabase(
            company={"id": "c1"},
            agent=_make_agent(llm_provider=None),
        )
        asyncio.run(
            _orch(supabase).run_turn(
                TurnRequest(user_message="hi", company_id="c1", session_id="s1", agent_id="agent-1")
            )
        )
        assert seen_provider[-1] == "openai"
    finally:
        cto.get_api_key_for_provider = orig_resolver  # type: ignore[assignment]
        restore_gr()
        restore_graph()
        restore_inv()


def test_api_key_resolved_once():
    _RecordingGuardrail.received = []
    calls: List[str] = []
    orig_resolver = cto.get_api_key_for_provider

    def _spy(provider=None, model=None):
        calls.append(provider)
        return "k"

    cto.get_api_key_for_provider = _spy  # type: ignore[assignment]
    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    async def _fake_invoke(**_k: Any):
        return {"response": "ok", "tokens_total": 0}

    restore_inv = _install_adapter("invoke_agent", _fake_invoke)
    try:
        supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
        asyncio.run(
            _orch(supabase).run_turn(
                TurnRequest(user_message="hi", company_id="c1", session_id="s1", agent_id="agent-1")
            )
        )
    finally:
        cto.get_api_key_for_provider = orig_resolver  # type: ignore[assignment]
        restore_gr()
        restore_graph()
        restore_inv()

    assert len(calls) == 1  # resolved exactly once per turn


def test_missing_api_key_raises_explicit_no_fallback():
    orig_resolver = cto.get_api_key_for_provider

    def _missing(provider=None, model=None):
        raise ValueError("missing key")

    cto.get_api_key_for_provider = _missing  # type: ignore[assignment]
    try:
        supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
        raised = False
        try:
            asyncio.run(
                _orch(supabase).run_turn(
                    TurnRequest(user_message="hi", company_id="c1", session_id="s1", agent_id="agent-1")
                )
            )
        except ValueError:
            raised = True
        assert raised  # explicit failure, no silent fallback
    finally:
        cto.get_api_key_for_provider = orig_resolver  # type: ignore[assignment]


# =========================================================================== #
# Guardrail block / fail-close
# =========================================================================== #
class _BlockingGuardrail:
    def __init__(self, agent_config, company_id):
        self.fail_close = True

    async def validate_input(self, text: str):
        return True, "BLOCKED_REASON", text


class _ExplodingGuardrail:
    def __init__(self, agent_config, company_id):
        self.fail_close = True

    async def validate_input(self, text: str):
        raise RuntimeError("guardrail boom")


def test_run_turn_blocked_short_circuits():
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    restore_gr = _install_guardrail(_BlockingGuardrail)

    invoked: List[Any] = []

    async def _fake_invoke(**k: Any):
        invoked.append(k)
        return {"response": "should-not-run", "tokens_total": 9}

    restore_inv = _install_adapter("invoke_agent", _fake_invoke)
    restore_graph = _stub_graph_acquire()
    try:
        result = asyncio.run(
            _orch(supabase).run_turn(
                TurnRequest(user_message="bad", company_id="c1", session_id="s1", agent_id="agent-1")
            )
        )
    finally:
        restore_gr()
        restore_inv()
        restore_graph()

    assert result.response == "BLOCKED_REASON"
    assert result.tokens_total == 0
    assert result.metrics is not None and result.metrics.end_time is not None
    assert invoked == []  # adapter never invoked


def test_stream_turn_blocked_emits_blocked_then_done():
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    restore_gr = _install_guardrail(_BlockingGuardrail)
    try:
        events = _drain_stream(
            _orch(supabase),
            TurnRequest(user_message="bad", company_id="c1", session_id="s1", agent_id="agent-1"),
        )
    finally:
        restore_gr()

    assert [e.type for e in events] == ["blocked", "done"]
    assert events[0].data == "BLOCKED_REASON"


def test_guardrail_exception_fail_closes_aggregate():
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    restore_gr = _install_guardrail(_ExplodingGuardrail)
    try:
        result = asyncio.run(
            _orch(supabase).run_turn(
                TurnRequest(user_message="x", company_id="c1", session_id="s1", agent_id="agent-1")
            )
        )
    finally:
        restore_gr()
    assert "segurança" in result.response  # fail-close security message
    assert result.tokens_total == 0


def test_guardrail_exception_fail_closes_stream():
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    restore_gr = _install_guardrail(_ExplodingGuardrail)
    try:
        events = _drain_stream(
            _orch(supabase),
            TurnRequest(user_message="x", company_id="c1", session_id="s1", agent_id="agent-1"),
        )
    finally:
        restore_gr()
    assert [e.type for e in events] == ["blocked", "done"]


# =========================================================================== #
# Recovery (D4)
# =========================================================================== #
class _FlakyInvoke:
    """Fake invoke_agent that raises a recoverable error N times then succeeds."""

    def __init__(self, fail_times: int, exc: Exception) -> None:
        self.fail_times = fail_times
        self.exc = exc
        self.calls = 0

    async def __call__(self, **_k: Any):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return {"response": "recovered", "tokens_total": 7}


def test_aggregate_recovery_retries_then_succeeds():
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    # OperationalError-style error: orchestrator detects via keyword ("ssl eof").
    flaky = _FlakyInvoke(fail_times=1, exc=Exception("server closed the connection: ssl eof"))
    restore_inv = _install_adapter("invoke_agent", flaky)

    invalidated: List[Any] = []
    orig_inv_cache = cto.invalidate_agent_graph_cache
    cto.invalidate_agent_graph_cache = lambda c, a: invalidated.append((c, a))  # type: ignore

    closed: List[int] = []

    async def _fake_close():
        closed.append(1)

    restore_close = _install_close_pool(_fake_close)
    try:
        result = asyncio.run(
            _orch(supabase).run_turn(
                TurnRequest(user_message="hi", company_id="c1", session_id="s1", agent_id="agent-1")
            )
        )
    finally:
        cto.invalidate_agent_graph_cache = orig_inv_cache  # type: ignore
        restore_gr()
        restore_graph()
        restore_inv()
        restore_close()

    assert result.response == "recovered"
    assert flaky.calls == 2  # one failure + one success
    assert invalidated == [("c1", "agent-1")]  # cache invalidated on recovery
    assert closed == [1]  # pool closed on recovery


def test_aggregate_recovery_exhausts_raises():
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    flaky = _FlakyInvoke(fail_times=99, exc=Exception("connection pool closed"))
    restore_inv = _install_adapter("invoke_agent", flaky)

    orig_inv_cache = cto.invalidate_agent_graph_cache
    cto.invalidate_agent_graph_cache = lambda c, a: None  # type: ignore

    async def _fake_close():
        return None

    restore_close = _install_close_pool(_fake_close)
    try:
        raised = False
        try:
            asyncio.run(
                _orch(supabase).run_turn(
                    TurnRequest(user_message="hi", company_id="c1", session_id="s1", agent_id="agent-1")
                )
            )
        except Exception:
            raised = True
        assert raised
        # 1 initial + TURN_MAX_RETRIES retries = 3 attempts total.
        assert flaky.calls == 1 + cto.TURN_MAX_RETRIES
    finally:
        cto.invalidate_agent_graph_cache = orig_inv_cache  # type: ignore
        restore_gr()
        restore_graph()
        restore_inv()
        restore_close()


def test_stream_recovery_before_first_token_retries():
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    state = {"calls": 0}

    async def _flaky_stream(**_k: Any):
        state["calls"] += 1
        if state["calls"] == 1:
            # Raise BEFORE yielding any token -> had_streamed=False -> retry OK.
            raise Exception("pool is closed")
            yield  # pragma: no cover
        for t in ["x", "y"]:
            yield t

    restore_stream = _install_adapter("stream_agent", _flaky_stream)

    orig_inv_cache = cto.invalidate_agent_graph_cache
    cto.invalidate_agent_graph_cache = lambda c, a: None  # type: ignore

    async def _fake_close():
        return None

    restore_close = _install_close_pool(_fake_close)
    try:
        events = _drain_stream(
            _orch(supabase),
            TurnRequest(user_message="hi", company_id="c1", session_id="s1", agent_id="agent-1"),
        )
    finally:
        cto.invalidate_agent_graph_cache = orig_inv_cache  # type: ignore
        restore_gr()
        restore_graph()
        restore_stream()
        restore_close()

    tokens = [e.data for e in events if e.type == "token"]
    assert tokens == ["x", "y"]  # retry delivered the stream
    assert events[-1].type == "done"
    assert not any(e.type == "error" for e in events)
    assert state["calls"] == 2


def test_stream_recovery_after_first_token_emits_error_no_rerun():
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    state = {"calls": 0}

    async def _flaky_stream(**_k: Any):
        state["calls"] += 1
        yield "first"  # had_streamed becomes True
        raise Exception("ssl eof: server closed")

    restore_stream = _install_adapter("stream_agent", _flaky_stream)
    try:
        events = _drain_stream(
            _orch(supabase),
            TurnRequest(user_message="hi", company_id="c1", session_id="s1", agent_id="agent-1"),
        )
    finally:
        restore_gr()
        restore_graph()
        restore_stream()

    types = [e.type for e in events]
    assert types == ["token", "error", "done"]
    assert events[0].data == "first"
    assert events[1].correlation_id is not None
    assert state["calls"] == 1  # NO re-run after first token


# =========================================================================== #
# Vision degradation (§5.6)
# =========================================================================== #
def test_vision_failure_degrades_to_user_message():
    _RecordingGuardrail.received = []
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent(vision_model="gpt-4o"))
    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    async def _fake_invoke(**_k: Any):
        return {"response": "ok", "tokens_total": 0}

    restore_inv = _install_adapter("invoke_agent", _fake_invoke)

    def _boom(*a, **k):
        raise RuntimeError("vision boom")

    orig_analyze = ChatTurnOrchestrator._analyze_image
    ChatTurnOrchestrator._analyze_image = staticmethod(_boom)  # type: ignore[assignment]
    try:
        asyncio.run(
            _orch(supabase).run_turn(
                TurnRequest(
                    user_message="just text",
                    company_id="c1",
                    session_id="s1",
                    agent_id="agent-1",
                    image_url="https://img/x.png",
                )
            )
        )
    finally:
        ChatTurnOrchestrator._analyze_image = orig_analyze  # type: ignore[assignment]
        restore_gr()
        restore_graph()
        restore_inv()

    # Vision failed -> enriched falls back to user_message; guardrail still ran.
    assert _RecordingGuardrail.received == ["just text"]


def test_vision_timeout_degrades_to_user_message():
    _RecordingGuardrail.received = []
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent(vision_model="gpt-4o"))
    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    async def _fake_invoke(**_k: Any):
        return {"response": "ok", "tokens_total": 0}

    restore_inv = _install_adapter("invoke_agent", _fake_invoke)

    # Force a tiny timeout so the (slow) vision call always trips it.
    orig_timeout = cto.VISION_TIMEOUT_SECONDS
    cto.VISION_TIMEOUT_SECONDS = 0.01  # type: ignore[assignment]

    import time as _time

    def _slow(*a, **k):
        _time.sleep(0.2)
        return "late desc"

    orig_analyze = ChatTurnOrchestrator._analyze_image
    ChatTurnOrchestrator._analyze_image = staticmethod(_slow)  # type: ignore[assignment]
    try:
        asyncio.run(
            _orch(supabase).run_turn(
                TurnRequest(
                    user_message="timed text",
                    company_id="c1",
                    session_id="s1",
                    agent_id="agent-1",
                    image_url="https://img/x.png",
                )
            )
        )
    finally:
        cto.VISION_TIMEOUT_SECONDS = orig_timeout  # type: ignore[assignment]
        ChatTurnOrchestrator._analyze_image = orig_analyze  # type: ignore[assignment]
        restore_gr()
        restore_graph()
        restore_inv()

    # Timeout -> degrade to user_message; never the late description.
    assert _RecordingGuardrail.received == ["timed text"]


# =========================================================================== #
# cache_hit
# =========================================================================== #
def test_cache_hit_reported_on_repeat_acquire():
    _RecordingGuardrail.received = []
    os.environ["OPENAI_API_KEY"] = "sk-test"
    gc._graphs_cache.clear()

    agent = _make_agent()
    supabase = FakeSupabase(company={"id": "c1"}, agent=agent)
    restore_gr = _install_guardrail(_RecordingGuardrail)

    created: List[int] = []

    async def _fake_create(**_k: Any):
        created.append(1)
        return object()

    restore_create = _install_create_agent_graph(_fake_create)

    seen_hits: List[Optional[bool]] = []

    async def _fake_invoke(**_k: Any):
        return {"response": "ok", "tokens_total": 0}

    restore_inv = _install_adapter("invoke_agent", _fake_invoke)

    # Wrap stop to capture cache_hit by reading instrumentation after each run.
    orch = _orch(supabase)

    # Monkeypatch _acquire_graph to record the cache_hit it computed.
    orig_acquire = orch._acquire_graph

    async def _spy_acquire(req, ctx, instr):
        g = await orig_acquire(req, ctx, instr)
        seen_hits.append(instr.cache_hit)
        return g

    orch._acquire_graph = _spy_acquire  # type: ignore[assignment]
    try:
        req = TurnRequest(user_message="hi", company_id="c1", session_id="s1", agent_id="agent-1")
        asyncio.run(orch.run_turn(req))  # first: miss
        asyncio.run(orch.run_turn(req))  # second: hit
    finally:
        restore_gr()
        restore_create()
        restore_inv()
        gc._graphs_cache.clear()

    assert seen_hits == [False, True]
    assert len(created) == 1  # only one graph actually built


# =========================================================================== #
# first_token latency (D6) — Sprint 5
# =========================================================================== #
def _capture_instr(orch, stop_calls: Optional[List[str]] = None):
    """Patch _make_instrumentation so each created TurnInstrumentation is
    captured. If `stop_calls` is given, every instr.stop(stage) appends `stage`
    to it (spy installed at creation, before any stage runs). Returns
    (captured_list, restore)."""
    captured: List[Any] = []
    orig = orch._make_instrumentation

    def _spy(req, *, mode):
        instr = orig(req, mode=mode)
        if stop_calls is not None:
            orig_stop = instr.stop

            def _spy_stop(stage, _orig=orig_stop, **extra):
                stop_calls.append(stage)
                return _orig(stage, **extra)

            instr.stop = _spy_stop  # type: ignore[assignment]
        captured.append(instr)
        return instr

    orch._make_instrumentation = _spy  # type: ignore[assignment]

    def _restore():
        orch._make_instrumentation = orig  # type: ignore[assignment]

    return captured, _restore


def test_first_token_latency_recorded_on_stream():
    """A successful stream records `first_token` exactly once, measured from
    just-before-consumption to the FIRST token (so it is ≤ invoke_total)."""
    _RecordingGuardrail.received = []
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    orch = _orch(supabase)
    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    async def _fake_stream(**_k: Any):
        for t in ["a", "b", "c"]:
            yield t

    restore_stream = _install_adapter("stream_agent", _fake_stream)
    # Spy on stop() to count how many times first_token is stopped.
    stop_calls: List[str] = []
    captured, restore_capture = _capture_instr(orch, stop_calls)
    try:
        events = _drain_stream(
            orch,
            TurnRequest(user_message="hi", company_id="c1", session_id="s1", agent_id="agent-1"),
        )
    finally:
        restore_gr()
        restore_graph()
        restore_stream()
        restore_capture()

    tokens = [e.data for e in events if e.type == "token"]
    assert tokens == ["a", "b", "c"]
    assert events[-1].type == "done"

    assert len(captured) == 1
    instr = captured[0]
    assert "first_token" in instr.stage_ms
    assert isinstance(instr.stage_ms["first_token"], float)
    assert instr.stage_ms["first_token"] >= 0.0
    # first_token measures up to the FIRST token, so ≤ the whole invoke_total.
    assert "invoke_total" in instr.stage_ms
    assert instr.stage_ms["first_token"] <= instr.stage_ms["invoke_total"]
    # Stopped exactly once (subsequent tokens must not re-stop).
    assert stop_calls.count("first_token") == 1


def test_first_token_absent_when_no_tokens():
    """An empty stream never stops first_token (key absent) yet ends cleanly."""
    _RecordingGuardrail.received = []
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    orch = _orch(supabase)
    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    async def _empty_stream(**_k: Any):
        return
        yield  # pragma: no cover — makes this an async generator

    restore_stream = _install_adapter("stream_agent", _empty_stream)
    captured, restore_capture = _capture_instr(orch)
    try:
        events = _drain_stream(
            orch,
            TurnRequest(user_message="hi", company_id="c1", session_id="s1", agent_id="agent-1"),
        )
    finally:
        restore_gr()
        restore_graph()
        restore_stream()
        restore_capture()

    assert [e.type for e in events] == ["done"]  # no tokens, clean done
    assert len(captured) == 1
    assert "first_token" not in captured[0].stage_ms


def test_correlation_id_stable_and_reused_across_stream_error():
    """A non-recoverable error after the first token surfaces the turn's
    correlation_id on the error event AND reuses it in the final emit/req."""
    _RecordingGuardrail.received = []
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    orch = _orch(supabase)
    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    async def _flaky_stream(**_k: Any):
        yield "first"  # had_streamed -> True
        raise ValueError("boom — not a pool/connection error")  # non-recoverable

    restore_stream = _install_adapter("stream_agent", _flaky_stream)
    captured, restore_capture = _capture_instr(orch)

    req = TurnRequest(user_message="hi", company_id="c1", session_id="s1", agent_id="agent-1")
    try:
        events = _drain_stream(orch, req)
    finally:
        restore_gr()
        restore_graph()
        restore_stream()
        restore_capture()

    types = [e.type for e in events]
    assert types == ["token", "error", "done"]
    err = events[1]
    assert err.correlation_id is not None
    # The same correlation_id is reused on req (stored in _make_instrumentation)
    # and carried by the captured instrumentation (no regeneration).
    assert err.correlation_id == req.correlation_id
    assert len(captured) == 1
    assert captured[0].correlation_id == req.correlation_id


def test_instrumentation_does_not_change_stream_output():
    """Logging puro: the yielded token values + final done match exactly what
    the fake produced — instrumentation adds/drops nothing (D6 invariant)."""
    _RecordingGuardrail.received = []
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    orch = _orch(supabase)
    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    produced = ["alpha", "beta", "gamma"]

    async def _fake_stream(**_k: Any):
        for t in produced:
            yield t

    restore_stream = _install_adapter("stream_agent", _fake_stream)
    try:
        events = _drain_stream(
            orch,
            TurnRequest(user_message="hi", company_id="c1", session_id="s1", agent_id="agent-1"),
        )
    finally:
        restore_gr()
        restore_graph()
        restore_stream()

    tokens = [e.data for e in events if e.type == "token"]
    assert tokens == produced  # exact sequence, nothing added/dropped
    assert [e.type for e in events] == ["token", "token", "token", "done"]


# =========================================================================== #
# P2-4 — orchestrator's resolved provider is authoritative for cache key + graph
# =========================================================================== #
def test_company_provider_written_back_into_agent_dict():
    """Agent with NO llm_provider but company has 'anthropic' → the orchestrator
    writes 'anthropic' back into the agent dict so BOTH the strong cache key
    (reads agent['llm_provider']) and the graph (reads agent_data['llm_provider'])
    see it — never wrongly falling to 'openai'."""
    _RecordingGuardrail.received = []
    os.environ["ANTHROPIC_API_KEY"] = "ak-test"
    agent = _make_agent(llm_provider=None)
    supabase = FakeSupabase(
        company={"id": "c1", "llm_provider": "anthropic"}, agent=agent
    )
    orch = _orch(supabase)

    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    captured: List[Dict[str, Any]] = []

    async def _fake_invoke(**kwargs: Any):
        # company_config is the agent dict passed through; capture its provider.
        captured.append(kwargs.get("company_config", {}))
        return {"response": "ok", "tokens_total": 0}

    restore_inv = _install_adapter("invoke_agent", _fake_invoke)
    try:
        asyncio.run(
            orch.run_turn(
                TurnRequest(
                    user_message="hi", company_id="c1", session_id="s1", agent_id="agent-1"
                )
            )
        )
    finally:
        restore_gr()
        restore_graph()
        restore_inv()

    # Writeback happened on the SAME dict the cache key + graph consume.
    assert agent["llm_provider"] == "anthropic"
    assert captured and captured[0].get("llm_provider") == "anthropic"
    # And the strong cache key now derives from "anthropic" (not "openai").
    key = gc.compute_graph_cache_key("c1", "agent-1", agent)
    key_openai = gc.compute_graph_cache_key(
        "c1", "agent-1", {**agent, "llm_provider": "openai"}
    )
    assert key != key_openai


# =========================================================================== #
# P1-1 — prompt-safety gate covers the WHOLE turn (zero LlamaGuard calls when off)
# =========================================================================== #
class _LlamaSpy:
    calls: List[Any] = []

    @classmethod
    def reset(cls):
        cls.calls = []

    async def validate_all(self, text, *, check_jailbreak=True, check_nsfw=False, fail_close=True):
        type(self).calls.append(text)
        return False, ""


def _install_llama_spy(spy):
    import app.services.llama_guard_service as lg

    orig = lg.get_llama_guard_service
    lg.get_llama_guard_service = lambda: spy  # type: ignore[assignment]

    def _restore():
        lg.get_llama_guard_service = orig  # type: ignore[assignment]

    return _restore


def test_gate_closed_when_security_disabled():
    """Gate 100% POR-AGENTE: o orchestrator dirige prompt_safety_enabled a partir
    de security_settings.enabled. Com enabled=False o gate FECHA, e o
    enforce_prompt_safety do grafo NÃO consulta o LlamaGuard (zero chamadas) —
    nada de baseline global rodando fora do opt-in do agente."""
    from app.agents.nodes import enforce_prompt_safety, prompt_safety_enabled

    _RecordingGuardrail.received = []
    _LlamaSpy.reset()
    os.environ["OPENAI_API_KEY"] = "sk-test"
    agent = _make_agent(security_settings={"enabled": False})
    supabase = FakeSupabase(company={"id": "c1"}, agent=agent)
    orch = _orch(supabase)

    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()
    restore_llama = _install_llama_spy(_LlamaSpy())

    async def _fake_invoke(**_k: Any):
        # Simula a chamada de enforce_prompt_safety dentro do grafo; com a
        # segurança do agente OFF o gate está fechado e o LlamaGuard NÃO é tocado.
        await enforce_prompt_safety("user message", label="user_input")
        return {"response": "ok", "tokens_total": 0}

    restore_inv = _install_adapter("invoke_agent", _fake_invoke)
    token = prompt_safety_enabled.set(True)  # prova que o orchestrator FECHA o gate
    try:
        asyncio.run(
            orch.run_turn(
                TurnRequest(
                    user_message="hi", company_id="c1", session_id="s1", agent_id="agent-1"
                )
            )
        )
    finally:
        prompt_safety_enabled.reset(token)
        restore_gr()
        restore_graph()
        restore_llama()
        restore_inv()

    # Segurança do agente OFF → gate fechado → ZERO LlamaGuard.
    assert _LlamaSpy.calls == []


def test_enabled_security_opens_the_gate_for_graph_path():
    """Gate 100% POR-AGENTE: com security_settings.enabled=True o gate ABRE, então
    o enforce_prompt_safety do grafo consulta o LlamaGuard sobre o user_input."""
    from app.agents.nodes import enforce_prompt_safety, prompt_safety_enabled

    _RecordingGuardrail.received = []
    _LlamaSpy.reset()
    os.environ["OPENAI_API_KEY"] = "sk-test"
    agent = _make_agent(security_settings={"enabled": True})
    supabase = FakeSupabase(company={"id": "c1"}, agent=agent)
    orch = _orch(supabase)

    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()
    restore_llama = _install_llama_spy(_LlamaSpy())

    async def _fake_invoke(**_k: Any):
        await enforce_prompt_safety("user message", label="user_input")
        return {"response": "ok", "tokens_total": 0}

    restore_inv = _install_adapter("invoke_agent", _fake_invoke)
    token = prompt_safety_enabled.set(True)
    try:
        asyncio.run(
            orch.run_turn(
                TurnRequest(
                    user_message="hi", company_id="c1", session_id="s1", agent_id="agent-1"
                )
            )
        )
    finally:
        prompt_safety_enabled.reset(token)
        restore_gr()
        restore_graph()
        restore_llama()
        restore_inv()

    assert _LlamaSpy.calls == ["user message"]  # gate open → consulted once.


# =========================================================================== #
# P1-2 — PromptSafetyError surfaces as BLOCKED (not token/response), not persisted
# =========================================================================== #
def test_stream_promptsafety_before_token_emits_blocked_then_done():
    from app.agents.nodes import PromptSafetyError

    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    orch = _orch(supabase)

    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    runs = {"n": 0}

    async def _fake_stream(**_k: Any):
        runs["n"] += 1
        raise PromptSafetyError("blocked")
        yield  # pragma: no cover — makes this an async generator

    restore_stream = _install_adapter("stream_agent", _fake_stream)
    try:
        events = _drain_stream(
            orch,
            TurnRequest(user_message="bad", company_id="c1", session_id="s1", agent_id="agent-1"),
        )
    finally:
        restore_gr()
        restore_graph()
        restore_stream()

    assert [e.type for e in events] == ["blocked", "done"]
    assert "segurança" in events[0].data  # the safe block message
    assert runs["n"] == 1  # no re-run


def test_stream_promptsafety_mid_token_emits_token_error_done():
    from app.agents.nodes import PromptSafetyError

    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    orch = _orch(supabase)

    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    runs = {"n": 0}

    async def _fake_stream(**_k: Any):
        runs["n"] += 1
        yield "Hello"
        raise PromptSafetyError("blocked mid-stream")

    restore_stream = _install_adapter("stream_agent", _fake_stream)
    try:
        events = _drain_stream(
            orch,
            TurnRequest(user_message="x", company_id="c1", session_id="s1", agent_id="agent-1"),
        )
    finally:
        restore_gr()
        restore_graph()
        restore_stream()

    assert [e.type for e in events] == ["token", "error", "done"]
    assert events[0].data == "Hello"
    assert runs["n"] == 1  # had_streamed → never re-runs


def test_aggregate_promptsafety_returns_blocked_tokens_zero():
    from app.agents.nodes import PromptSafetyError

    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    orch = _orch(supabase)

    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    async def _fake_invoke(**_k: Any):
        raise PromptSafetyError("blocked")

    restore_inv = _install_adapter("invoke_agent", _fake_invoke)
    try:
        result = asyncio.run(
            orch.run_turn(
                TurnRequest(user_message="bad", company_id="c1", session_id="s1", agent_id="agent-1")
            )
        )
    finally:
        restore_gr()
        restore_graph()
        restore_inv()

    # Blocked-shaped TurnResult: tokens_total=0 + the safe message.
    assert result.tokens_total == 0
    assert "segurança" in result.response
    assert result.metrics is not None and result.metrics.end_time is not None


# =========================================================================== #
# C1 Fase 1 — post-turn persistence opt-in, guarded by conversation_store (AC7)
# =========================================================================== #
class _RecordingStore:
    """Fake ConversationStore that records persist_turn calls (no DB)."""

    def __init__(self, conversation=None) -> None:
        self._conversation = conversation
        self.persist_turn_calls: List[Dict[str, Any]] = []

    async def persist_turn(self, **kwargs: Any) -> None:
        self.persist_turn_calls.append(kwargs)


def _orch_with_store(supabase, store) -> ChatTurnOrchestrator:
    """Orchestrator with an explicit store injected (handoff/billing left None so
    evaluate_pre_turn is NOT in play — these tests target run/stream persist)."""
    return ChatTurnOrchestrator(
        supabase_client=supabase,
        qdrant_service=None,
        conversation_store=store,
        billing_gate=None,
        handoff_policy=None,
    )


def test_run_turn_with_store_persists_on_success():
    _RecordingGuardrail.received = []
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    store = _RecordingStore()
    orch = _orch_with_store(supabase, store)

    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    async def _fake_invoke(**_k: Any):
        return {"response": "the answer", "tokens_total": 3}

    restore_inv = _install_adapter("invoke_agent", _fake_invoke)
    try:
        asyncio.run(
            orch.run_turn(
                TurnRequest(
                    user_message="hi",
                    company_id="c1",
                    session_id="s1",
                    agent_id="agent-1",
                    persist_user_message=True,
                    assistant_message_id="amid-7",
                )
            )
        )
    finally:
        restore_gr()
        restore_graph()
        restore_inv()

    assert len(store.persist_turn_calls) == 1
    call = store.persist_turn_calls[0]
    assert call["assistant_message"] == "the answer"
    assert call["persist_user_message"] is True
    assert call["assistant_message_id"] == "amid-7"
    assert call["user_message"] == "hi"


def test_run_turn_without_store_does_not_persist():
    _RecordingGuardrail.received = []
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    orch = _orch(supabase)  # NO store

    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    async def _fake_invoke(**_k: Any):
        return {"response": "the answer", "tokens_total": 3}

    restore_inv = _install_adapter("invoke_agent", _fake_invoke)
    try:
        result = asyncio.run(
            orch.run_turn(
                TurnRequest(user_message="hi", company_id="c1", session_id="s1", agent_id="agent-1")
            )
        )
    finally:
        restore_gr()
        restore_graph()
        restore_inv()

    # No store => seam persists nothing; response still returned.
    assert orch.conversation_store is None
    assert result.response == "the answer"


def test_run_turn_blocked_with_store_does_not_persist():
    """BLOCKED (guardrail) must NOT persist even with a store (AC4/AC7)."""
    _RecordingGuardrail.received = []
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    store = _RecordingStore()
    orch = _orch_with_store(supabase, store)

    restore_gr = _install_guardrail(_BlockingGuardrail)
    restore_graph = _stub_graph_acquire()

    async def _fake_invoke(**_k: Any):
        return {"response": "should-not-run", "tokens_total": 9}

    restore_inv = _install_adapter("invoke_agent", _fake_invoke)
    try:
        result = asyncio.run(
            orch.run_turn(
                TurnRequest(user_message="bad", company_id="c1", session_id="s1", agent_id="agent-1")
            )
        )
    finally:
        restore_gr()
        restore_graph()
        restore_inv()

    assert result.response == "BLOCKED_REASON"
    assert store.persist_turn_calls == []  # blocked turn is never persisted


def test_stream_turn_with_store_persists_after_clean_loop():
    _RecordingGuardrail.received = []
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    store = _RecordingStore()
    orch = _orch_with_store(supabase, store)

    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    async def _fake_stream(**_k: Any):
        for t in ["Hel", "lo!"]:
            yield t

    restore_stream = _install_adapter("stream_agent", _fake_stream)
    try:
        events = _drain_stream(
            orch,
            TurnRequest(
                user_message="hi",
                company_id="c1",
                session_id="s1",
                agent_id="agent-1",
                assistant_message_id="amid-9",
            ),
        )
    finally:
        restore_gr()
        restore_graph()
        restore_stream()

    assert events[-1].type == "done"
    assert len(store.persist_turn_calls) == 1
    call = store.persist_turn_calls[0]
    # Orchestrator is the SINGLE SOURCE of the streamed text (accumulated here).
    assert call["assistant_message"] == "Hello!"
    assert call["assistant_message_id"] == "amid-9"
    # /chat/stream default: persist_user_message stays False.
    assert call["persist_user_message"] is False


def test_stream_turn_without_store_does_not_persist():
    _RecordingGuardrail.received = []
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    orch = _orch(supabase)  # NO store

    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    async def _fake_stream(**_k: Any):
        yield "x"

    restore_stream = _install_adapter("stream_agent", _fake_stream)
    try:
        events = _drain_stream(
            orch,
            TurnRequest(user_message="hi", company_id="c1", session_id="s1", agent_id="agent-1"),
        )
    finally:
        restore_gr()
        restore_graph()
        restore_stream()

    assert [e.data for e in events if e.type == "token"] == ["x"]
    assert orch.conversation_store is None  # nothing to persist into


def test_stream_turn_blocked_with_store_does_not_persist():
    _RecordingGuardrail.received = []
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    store = _RecordingStore()
    orch = _orch_with_store(supabase, store)

    restore_gr = _install_guardrail(_BlockingGuardrail)
    try:
        events = _drain_stream(
            orch,
            TurnRequest(user_message="bad", company_id="c1", session_id="s1", agent_id="agent-1"),
        )
    finally:
        restore_gr()

    assert [e.type for e in events] == ["blocked", "done"]
    assert store.persist_turn_calls == []  # blocked stream never persists


def test_whatsapp_guard_async_client_without_store_does_not_persist():
    """AC7 / G2-fragility: orchestrator built WITH async_supabase_client but
    WITHOUT conversation_store must NOT auto-persist. Closes the risk of a future
    caller passing an async client and accidentally triggering persistence."""
    _RecordingGuardrail.received = []
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    orch = ChatTurnOrchestrator(
        supabase_client=supabase,
        qdrant_service=None,
        async_supabase_client=object(),  # present, like process_message passes
        conversation_store=None,  # explicit DRY: no store -> no auto-persist
        billing_gate=None,
        handoff_policy=None,
    )

    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    async def _fake_invoke(**_k: Any):
        return {"response": "the answer", "tokens_total": 3}

    restore_inv = _install_adapter("invoke_agent", _fake_invoke)
    try:
        asyncio.run(
            orch.run_turn(
                TurnRequest(user_message="hi", company_id="c1", session_id="s1", agent_id="agent-1")
            )
        )
    finally:
        restore_gr()
        restore_graph()
        restore_inv()

    # The async client alone NEVER materializes a store (no auto-persist).
    assert orch.conversation_store is None
    assert orch.billing_gate is None
    assert orch.handoff_policy is None


def test_stream_turn_disconnect_mid_stream_does_not_persist_partial():
    """G5: client disconnects mid-stream (consumer raises GeneratorExit when the
    async generator is closed). The seam must NOT persist a partial turn — the
    persist lives after the clean break, which is never reached."""
    _RecordingGuardrail.received = []
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    store = _RecordingStore()
    orch = _orch_with_store(supabase, store)

    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    async def _fake_stream(**_k: Any):
        # Emit several tokens; the consumer will abandon after the 2nd.
        for t in ["a", "b", "c", "d"]:
            yield t

    restore_stream = _install_adapter("stream_agent", _fake_stream)

    async def _consume_then_disconnect():
        agen = orch.stream_turn(
            TurnRequest(user_message="hi", company_id="c1", session_id="s1", agent_id="agent-1")
        )
        seen = 0
        async for ev in agen:
            if ev.type == "token":
                seen += 1
                if seen == 2:
                    # Simulate disconnect: close the generator mid-stream.
                    await agen.aclose()
                    break
        return seen

    try:
        seen = asyncio.run(_consume_then_disconnect())
    finally:
        restore_gr()
        restore_graph()
        restore_stream()

    assert seen == 2  # disconnected after 2 tokens
    assert store.persist_turn_calls == []  # NO partial persist (G5)


def test_persist_turn_reuses_pre_turn_conversation_no_reload():
    """D6/G2: persist_turn reuses self._pre_turn_conversation (set by
    evaluate_pre_turn) — zero re-load on the happy path."""
    _RecordingGuardrail.received = []
    os.environ["OPENAI_API_KEY"] = "sk-test"
    supabase = FakeSupabase(company={"id": "c1"}, agent=_make_agent())
    store = _RecordingStore()
    orch = _orch_with_store(supabase, store)

    cached_conv = {"id": "conv-cached", "status": "open"}
    orch._pre_turn_conversation = cached_conv  # as evaluate_pre_turn would set

    restore_gr = _install_guardrail(_RecordingGuardrail)
    restore_graph = _stub_graph_acquire()

    async def _fake_invoke(**_k: Any):
        return {"response": "ok", "tokens_total": 1}

    restore_inv = _install_adapter("invoke_agent", _fake_invoke)
    try:
        asyncio.run(
            orch.run_turn(
                TurnRequest(user_message="hi", company_id="c1", session_id="s1", agent_id="agent-1")
            )
        )
    finally:
        restore_gr()
        restore_graph()
        restore_inv()

    assert len(store.persist_turn_calls) == 1
    # The cached conversation is handed to persist_turn for reuse (no re-load).
    assert store.persist_turn_calls[0]["conversation"] is cached_conv
