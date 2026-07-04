"""
Sprint 0 — CHARACTERIZATION tests for the chat-turn code paths.

These tests PIN the CURRENT behavior of the aggregated path
(`LangChainService.process_message`), the streaming SSE wire format
(`app/api/chat.py:chat_stream`), the guardrail (`SmithGuardrail.validate_input`)
and the graph cache (`get_or_create_graph`) BEFORE the ChatTurnOrchestrator
refactor (SPEC 20260529_172113-738e71). They are the regression net that proves
later sprints (1) do not break wire contracts and (2) DO fix the documented
divergences.

IMPORTANT — these are TEST-ONLY; no production code is touched. Several
assertions intentionally pin a *current* behavior that the SPEC will flip in a
later sprint. Those are tagged `# CHARACTERIZATION (bug to flip in Sprint N)`
and assert the behavior as it exists TODAY so the suite stays green now.

Conventions mirror tests/services/test_ucp_invalidation.py:
  - NO pytest-asyncio; async is driven with asyncio.run(...).
  - Plain asserts; fakes/stubs injected as args.
  - Env vars are seeded by tests/services/conftest.py BEFORE importing app.*
  - NO external service is touched (Supabase, LLM, Qdrant, Groq all faked).
"""

from __future__ import annotations

import asyncio
import json
import types
from typing import Any, Dict, List, Optional

import app.services.langchain_service as lcs
from app.agents.guardrails import SmithGuardrail
from app.services.graph_cache import RUNTIME_GRAPH_VERSION, compute_graph_cache_key
from app.services.langchain_service import LangChainService, get_or_create_graph


# NOTE (Fase 4b / PR-4): the lazy `app.api.webhook` bootstrap + the D9 facade
# regression tests were removed together with the webhook's sync->async proxy
# and module-level conversation_store singleton. The ConversationStore contract
# against the REAL AsyncSupabaseClient shape is covered by tests/services/
# test_conversation_store.py (V1), and the WhatsApp turn now lives in
# app.services.whatsapp_turn_service (tests/services/test_whatsapp_turn_service.py).


# =========================================================================== #
# Shared fakes
# =========================================================================== #
class _Result:
    def __init__(self, data: Any) -> None:
        self.data = data


class _AgentQuery:
    """Chainable query mirroring _get_raw_agent's call chain, counting calls."""

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
    """Minimal stand-in for the sync DB wrapper used by LangChainService."""

    def __init__(self, company: Optional[Dict[str, Any]], agent: Optional[Dict[str, Any]]) -> None:
        self.company = company
        self.agent = agent
        self.client = _FakeClient(self)
        # Instrumentation for the query-count baseline (priority 5).
        self.query_count = 0
        self.get_company_count = 0
        self.history_count = 0
        self.table_calls: List[str] = []

    def get_company(self, company_id: str) -> Optional[Dict[str, Any]]:
        self.get_company_count += 1
        return self.company

    def get_conversation_history(self, **_k: Any) -> List[Dict[str, str]]:
        self.history_count += 1
        return []


class _RecordingGuardrail:
    """Fake guardrail that records the exact text validate_input received."""

    received: List[str] = []

    def __init__(self, agent_config: Dict[str, Any], company_id: str) -> None:
        self.fail_close = True

    async def validate_input(self, text: str):
        _RecordingGuardrail.received.append(text)
        # (is_blocked, reason, sanitized_text) — passthrough, no masking.
        return False, "", text


def _make_service() -> LangChainService:
    # Build the instance WITHOUT running __init__: the real __init__ constructs
    # an EncryptionService (Fernet) and OpenAIEmbeddings client, neither of which
    # is exercised by process_message under our fakes. We set only the attributes
    # the aggregated path actually touches.
    svc = LangChainService.__new__(LangChainService)
    svc.supabase = None  # tests override per-case
    svc.qdrant = None
    return svc


def _agents_module():
    """Return the live `app.agents` module object from sys.modules.

    NOTE: other test packages (tests/agents/*/conftest.py) seed STUB modules into
    sys.modules for langchain/langgraph/app.* to test in isolation. Those stubs
    persist for the whole session and can shadow the real `app.agents`, dropping
    its `invoke_agent`/`create_agent_graph` attributes. To stay robust regardless
    of suite ordering we set the fake attribute on WHATEVER `app.agents` object is
    currently registered (importing it on demand), instead of relying on the real
    attributes being present.
    """
    import importlib

    return importlib.import_module("app.agents")


def _install_api_key_resolver():
    """Ensure `app.core.utils.get_api_key_for_provider` accepts (provider, model).

    process_message resolves it lazily (`from app.core.utils import
    get_api_key_for_provider`). Sibling test packages stub `app.core.utils` in
    sys.modules with a 1-arg lambda, which would raise inside process_message.
    We install a 2-arg resolver on the live module and restore it afterward.
    """
    import importlib

    utils = importlib.import_module("app.core.utils")
    had_attr = hasattr(utils, "get_api_key_for_provider")
    orig = getattr(utils, "get_api_key_for_provider", None)

    def _resolver(provider=None, model=None):
        return "sk-test-resolved"

    utils.get_api_key_for_provider = _resolver  # type: ignore[assignment]

    def _restore() -> None:
        if had_attr:
            utils.get_api_key_for_provider = orig  # type: ignore[assignment]
        elif hasattr(utils, "get_api_key_for_provider"):
            delattr(utils, "get_api_key_for_provider")

    return _restore


def _install_invoke_agent(monkey_response: Dict[str, Any]):
    """Stub app.agents.invoke_agent (imported locally inside process_message via
    `from app.agents import invoke_agent`).

    Returns (captured_kwargs, restore_callable).
    """
    agents_pkg = _agents_module()
    captured: List[Dict[str, Any]] = []

    async def _fake_invoke(**kwargs: Any) -> Dict[str, Any]:
        captured.append(kwargs)
        return monkey_response

    had_attr = hasattr(agents_pkg, "invoke_agent")
    orig = getattr(agents_pkg, "invoke_agent", None)
    agents_pkg.invoke_agent = _fake_invoke  # type: ignore[assignment]

    def _restore() -> None:
        if had_attr:
            agents_pkg.invoke_agent = orig  # type: ignore[assignment]
        else:
            if hasattr(agents_pkg, "invoke_agent"):
                delattr(agents_pkg, "invoke_agent")

    return captured, _restore


def _install_create_agent_graph(fake):
    """Stub app.agents.create_agent_graph (imported locally inside
    get_or_create_graph via `from app.agents import create_agent_graph`).

    Returns a restore callable. Robust to sys.modules stub pollution.
    """
    agents_pkg = _agents_module()
    had_attr = hasattr(agents_pkg, "create_agent_graph")
    orig = getattr(agents_pkg, "create_agent_graph", None)
    agents_pkg.create_agent_graph = fake  # type: ignore[assignment]

    def _restore() -> None:
        if had_attr:
            agents_pkg.create_agent_graph = orig  # type: ignore[assignment]
        else:
            if hasattr(agents_pkg, "create_agent_graph"):
                delattr(agents_pkg, "create_agent_graph")

    return _restore


# --------------------------------------------------------------------------- #
# Sprint 2 plumbing: process_message now delegates to ChatTurnOrchestrator,
# which imports SmithGuardrail locally from `app.agents.guardrails` and acquires
# the graph via `app.services.graph_cache.get_or_create_graph` (which calls
# `app.agents.create_agent_graph`). So these tests must patch at the
# orchestrator-visible locations, NOT at `lcs.*`.
# --------------------------------------------------------------------------- #
def _install_guardrail_cls(monkeypatch_cls):
    """Patch SmithGuardrail where the orchestrator imports it (local import from
    app.agents.guardrails). Returns a restore callable."""
    import app.agents.guardrails as gr

    orig = gr.SmithGuardrail
    gr.SmithGuardrail = monkeypatch_cls  # type: ignore[assignment]

    def _restore() -> None:
        gr.SmithGuardrail = orig  # type: ignore[assignment]

    return _restore


# =========================================================================== #
# PRIORITY 1 — SSE wire format (byte-for-byte)
# =========================================================================== #
# The chat_stream generator in app/api/chat.py is too coupled (FastAPI deps,
# billing, widget security) to invoke end-to-end. We pin the EXACT serialization
# the code produces by replicating the literal `json.dumps(...)` + f-string the
# source uses, and asserting the resulting bytes. A future serializer (the
# orchestrator's StreamEvent -> SSE adapter) MUST reproduce these byte strings.
#
#   token   -> data: {"token": <t>}\n\n                  (chat.py:940)
#   blocked -> data: {"token": <msg>, "blocked": true}\n\n (chat.py:882, 907)
#   error   -> data: {"error": <e>, "correlationId": <cid>}\n\n (chat.py:1004-1011)
#   done    -> data: [DONE]\n\n                           (chat.py:1014)
def _sse_token(token: str) -> str:
    # Mirrors chat.py:940  -> data = json.dumps({"token": token}); f"data: {data}\n\n"
    return f"data: {json.dumps({'token': token})}\n\n"


def _sse_blocked(msg: str) -> str:
    # Mirrors chat.py:882 / 907 -> json.dumps({"token": msg, "blocked": True})
    return f"data: {json.dumps({'token': msg, 'blocked': True})}\n\n"


def _sse_error(err: str, cid: str) -> str:
    # Mirrors chat.py:1004-1011 -> json.dumps({"error": err, "correlationId": cid})
    return f"data: {json.dumps({'error': err, 'correlationId': cid})}\n\n"


def _sse_done() -> str:
    # Mirrors chat.py:1014
    return "data: [DONE]\n\n"


def test_sse_token_wire_format_is_pinned():
    assert _sse_token("Olá") == 'data: {"token": "Ol\\u00e1"}\n\n'
    assert _sse_token("hi") == 'data: {"token": "hi"}\n\n'
    # The terminator is exactly two newlines and there is a single space after "data:".
    assert _sse_token("x").startswith("data: ")
    assert _sse_token("x").endswith("\n\n")


def test_sse_blocked_wire_format_is_pinned():
    # Note: json.dumps emits lowercase `true` for Python True; key order is
    # insertion order (token, then blocked).
    assert (
        _sse_blocked("Mensagem bloqueada")
        == 'data: {"token": "Mensagem bloqueada", "blocked": true}\n\n'
    )


def test_sse_error_wire_format_is_pinned():
    assert (
        _sse_error("Erro interno no stream", "cid-123")
        == 'data: {"error": "Erro interno no stream", "correlationId": "cid-123"}\n\n'
    )


def test_sse_done_wire_format_is_pinned():
    assert _sse_done() == "data: [DONE]\n\n"


def _read_chat_stream_source() -> str:
    """Read the `chat_stream` function source straight from app/api/chat.py on disk.

    We deliberately do NOT `import app.api.chat`: sibling test packages
    (tests/agents/*) seed STUB modules for `app.services`/`langchain` into
    sys.modules that persist for the session and break that import. Reading the
    file is robust to suite ordering and is sufficient to pin the literal wire
    serialization (the bytes are produced by literal json.dumps + f-strings).
    """
    import pathlib

    chat_path = pathlib.Path(lcs.__file__).resolve().parents[1] / "api" / "chat.py"
    full = chat_path.read_text(encoding="utf-8")
    start = full.index("async def chat_stream")
    return full[start:]


def _read_sse_renderer_source() -> str:
    """Read the sse_renderer source from disk (the SSE byte format lives here).

    Fase 2 migrated the StreamEvent → SSE serialization out of chat_stream and
    into the renderer (the single home for the wire mapping). The byte-format
    guards therefore pin the literals in sse_renderer.py, not chat.py.
    """
    import pathlib

    sse_path = (
        pathlib.Path(lcs.__file__).resolve().parents[1]
        / "services"
        / "turn_ports"
        / "renderers"
        / "sse_renderer.py"
    )
    return sse_path.read_text(encoding="utf-8")


def _read_chat_stream_function_only() -> str:
    """Slice ONLY the `chat_stream` function body (up to the next top-level def).

    `_read_chat_stream_source` returns everything from `async def chat_stream`
    to EOF, which includes trailing endpoints (e.g. `delete_session`) that
    legitimately touch `table("conversations")`. The Sprint-4 "no inline X"
    asserts must be scoped to chat_stream alone, so we cut at the next decorator
    / top-level (column-0) `def`/`async def` after the function header.
    """
    src = _read_chat_stream_source()
    # Skip past the header line, then find the next top-level boundary.
    body_start = src.index("\n") + 1
    import re

    m = re.search(r"\n(@router\.|def |async def )", src[body_start:])
    if m:
        return src[: body_start + m.start()]
    return src


def test_sse_wire_strings_match_chat_source_literals():
    """Guard: the helpers above must match the literal serialization in chat.py.

    We read the source of chat_stream and assert the load-bearing literals are
    still present, so this characterization test fails loudly if someone changes
    the wire format in a future edit (the whole point of pinning it).
    """
    # Fase 2: the StreamEvent → SSE serialization moved to the sse_renderer (the
    # single home for the wire mapping). The load-bearing literals are pinned
    # there now.
    src = _read_sse_renderer_source()
    # token event
    assert "json.dumps({'token': ev.data})" in src
    # blocked event
    assert "json.dumps({'token': ev.data, 'blocked': True})" in src
    # error event (static message via _STREAM_ERROR_TEXT; correlationId on the wire)
    assert '_STREAM_ERROR_TEXT = "Erro interno no stream"' in src
    assert '"correlationId": correlation_id' in src
    # done sentinel
    assert 'yield "data: [DONE]\\n\\n"' in src


def test_sse_error_branch_does_not_leak_exception_text():
    """Guard: the error EVENT branch must emit the STATIC message, never the
    orchestrator's exception text (`ev.error`).

    `test_sse_wire_strings_match_chat_source_literals` cannot catch a regression
    that serializes `ev.error` to the wire, because the static literal
    ``"error": "Erro interno no stream"`` also appears in the safety-net
    ``except`` block — its presence elsewhere masks a leak in the error branch.
    So we slice out the ``elif ev.type == "error":`` block specifically and
    assert (a) the static message is the one serialized there and (b) the raw
    exception text (`ev.error`) is never put on the wire (it would leak internal
    DB DSNs / stack details to the frontend and the n8n proxy).

    Fase 2: the error branch lives in the sse_renderer now (the single home for
    the StreamEvent → SSE mapping), so we slice it from there.
    """
    src = _read_sse_renderer_source()
    start = src.index('elif ev.type == "error":')
    # The branch ends at the `# ev.type == "done"` comment that follows it.
    end = src.index('ev.type == "done"', start)
    branch = src[start:end]

    # The static message constant is what gets serialized (no exception text).
    assert "_STREAM_ERROR_TEXT" in branch
    # The exception payload must NOT be serialized to the client.
    assert "ev.error" not in branch


# =========================================================================== #
# PRIORITY 2 — Guardrail enabled=false STILL calls the external safety service
# =========================================================================== #
class _SpySafety:
    """Spy replacing SmithGuardrail.safety_service.validate_all / _call_model."""

    def __init__(self) -> None:
        self.validate_all_calls: List[Dict[str, Any]] = []
        self.call_model_calls: List[Any] = []

    async def validate_all(self, message, *, check_jailbreak=True, check_nsfw=False, fail_close=True):
        self.validate_all_calls.append(
            {
                "message": message,
                "check_jailbreak": check_jailbreak,
                "check_nsfw": check_nsfw,
                "fail_close": fail_close,
            }
        )
        # Mimic "safe": (is_unsafe, reason)
        return False, ""

    async def _call_model(self, model, message):
        self.call_model_calls.append((model, message))
        return "BENIGN"


def test_guardrail_disabled_makes_zero_safety_calls():
    # Gate 100% POR-AGENTE: com security_settings.enabled == False NADA roda —
    # ZERO chamadas ao Prompt Guard. Texto limpo passa inalterado.
    guardrail = SmithGuardrail(
        agent_config={"security_settings": {"enabled": False}},
        company_id="company-1",
    )
    spy = _SpySafety()  # returns safe (is_unsafe=False)
    guardrail.safety_service = spy  # type: ignore[assignment]

    is_blocked, reason, sanitized = asyncio.run(guardrail.validate_input("hello world"))

    # Segurança do agente OFF → ZERO chamadas de segurança.
    assert spy.validate_all_calls == []
    assert is_blocked is False
    assert reason == ""
    assert sanitized == "hello world"


def test_guardrail_disabled_does_not_block_even_unsafe_input():
    # Com a segurança do agente OFF, nem um input "unsafe" é bloqueado — o
    # guardrail simplesmente NÃO roda. Quem decide ligar é o agente do cliente.
    guardrail = SmithGuardrail(
        agent_config={"security_settings": {"enabled": False}},
        company_id="company-1",
    )

    class _UnsafeSpy(_SpySafety):
        async def validate_all(self, message, *, check_jailbreak=True, check_nsfw=False, fail_close=True):
            self.validate_all_calls.append({"message": message})
            return True, "jailbreak"

    spy = _UnsafeSpy()
    guardrail.safety_service = spy  # type: ignore[assignment]

    is_blocked, reason, sanitized = asyncio.run(guardrail.validate_input("some text"))

    assert spy.validate_all_calls == []  # guardrail NÃO rodou
    assert is_blocked is False
    assert sanitized == "some text"


def test_guardrail_enabled_runs_safety_service_exactly_once():
    # Pins the enabled-path contract: validate_all runs exactly once per call.
    guardrail = SmithGuardrail(
        agent_config={
            "security_settings": {
                "enabled": True,
                "check_secret_keys": False,
                "check_jailbreak": True,
                "check_nsfw": False,
                "custom_regex": [],
                "pii_action": "off",
                "check_urls": False,
            }
        },
        company_id="company-1",
    )
    spy = _SpySafety()
    guardrail.safety_service = spy  # type: ignore[assignment]

    is_blocked, _, sanitized = asyncio.run(guardrail.validate_input("enriched text"))

    assert len(spy.validate_all_calls) == 1
    assert spy.validate_all_calls[0]["message"] == "enriched text"
    assert is_blocked is False
    assert sanitized == "enriched text"


# =========================================================================== #
# PRIORITY 3 — Guardrail/vision ORDER divergence
# =========================================================================== #
def test_aggregated_path_guards_ENRICHED_message_after_vision():
    # FLIPPED in Sprint 2 (D2; SPEC §0): the AGGREGATED path now delegates to the
    # ChatTurnOrchestrator, which runs vision BEFORE the guardrail and validates
    # the ENRICHED message (matching the streaming path). So with an image_url
    # present and a resolvable vision model, the guardrail must receive the
    # "[CONTEXTO VISUAL]" enriched text, NOT the raw user_message. This replaces
    # the old characterization that pinned guard-before-vision on the raw text.
    import os as _os

    from app.services.chat_turn_orchestrator import ChatTurnOrchestrator

    _RecordingGuardrail.received = []

    company = {"id": "company-1"}
    agent = {
        "id": "agent-1",
        "llm_provider": "openai",
        "llm_model": "gpt-4o",
        "security_settings": {"enabled": True},
        "vision_model": "gpt-4o",  # resolvable -> vision enriches the message
        "updated_at": "2026-05-29T00:00:00",
    }
    supabase = FakeSupabase(company=company, agent=agent)
    svc = _make_service()
    svc.supabase = supabase
    svc.qdrant = None

    # Ensure the vision key resolves so vision actually runs.
    _os.environ["OPENAI_API_KEY"] = "sk-test"

    # Patch the guardrail where the orchestrator imports it, stub graph creation
    # and invoke_agent, and make vision deterministic.
    _restore_api_key = _install_api_key_resolver()
    _restore_gr = _install_guardrail_cls(_RecordingGuardrail)

    async def _fake_create(**_k: Any):
        return object()

    _restore_create = _install_create_agent_graph(_fake_create)
    captured, _restore_invoke = _install_invoke_agent(
        {"response": "ok", "tokens_total": 3}
    )

    orig_analyze = ChatTurnOrchestrator._analyze_image
    ChatTurnOrchestrator._analyze_image = staticmethod(  # type: ignore[assignment]
        lambda *a, **k: "DESC_FROM_IMAGE"
    )
    try:
        response, metrics = asyncio.run(
            svc.process_message(
                user_message="RAW question",
                company_id="company-1",
                user_id="user-1",
                session_id="sess-1",
                image_url="https://img/x.png",
                agent_id="agent-1",
            )
        )
    finally:
        ChatTurnOrchestrator._analyze_image = orig_analyze  # type: ignore[assignment]
        _restore_api_key()
        _restore_gr()
        _restore_create()
        _restore_invoke()

    assert response == "ok"
    # The guardrail received the ENRICHED message exactly once.
    assert len(_RecordingGuardrail.received) == 1
    assert "[CONTEXTO VISUAL]" in _RecordingGuardrail.received[0]
    assert "DESC_FROM_IMAGE" in _RecordingGuardrail.received[0]
    assert "RAW question" in _RecordingGuardrail.received[0]
    # invoke got the same enriched message.
    assert "[CONTEXTO VISUAL]" in captured[0]["user_message"]


def test_streaming_path_delegates_vision_and_guardrail_to_orchestrator():
    # FLIPPED in Sprint 3 (D2/D3 wiring): chat_stream no longer runs vision or
    # the guardrail inline. Both now live in ChatTurnOrchestrator._execute_turn
    # (vision BEFORE guardrail on the ENRICHED message), reached via
    # `orch.stream_turn(...)`. So the old structural pins (inline
    # `enriched_message =` build + `guardrail.validate_input(enriched_message)`
    # in chat.py source) no longer hold; we assert the delegation instead. The
    # enriched-before-guardrail ordering is now proven by the orchestrator tests
    # (tests/services/test_chat_turn_orchestrator.py).
    # Fase 2: chat_stream no longer consumes the orchestrator stream inline. It
    # builds a TurnRequest, resolves the pre-turn gate via the TurnRunner seam and
    # delegates the wire to render_sse (which owns the StreamEvent → SSE mapping,
    # reached via the orchestrator's stream_turn).
    src = _read_chat_stream_source()
    assert "TurnRequest(" in src
    assert "resolve_pre_turn(" in src
    assert "render_sse(" in src
    # The inline vision/guardrail are GONE from the streaming endpoint.
    assert "validate_input(enriched_message)" not in src
    assert "[CONTEXTO VISUAL]" not in src


# =========================================================================== #
# SPRINT 4 (Fase 2) — /chat/stream wired as a thin shell (D5/G1/D3, §6.5).
# Source-asserts (the harness reads the chat_stream SOURCE rather than driving
# it through TestClient, §8.6/AC9) + orchestrator-level render assertions for
# each TurnOutcome with fakes. We pin that handoff/paywall/persistence inline
# and the agent existence pre-check are GONE, and that evaluate_pre_turn is the
# single pre-turn gate, evaluated AFTER widget-security (D5/G1, AC1/AC11).
# =========================================================================== #
def test_chat_stream_calls_evaluate_pre_turn_as_single_gate():
    # AC1/AC2 (Fase 2): the stream shell delegates handoff+paywall to the TurnRunner
    # seam (build_http_turn_runner + resolve_pre_turn — one gate) and renders the
    # neutral event via render_sse, instead of branching on TurnOutcome inline.
    src = _read_chat_stream_function_only()
    assert "build_http_turn_runner(" in src
    assert "resolve_pre_turn(" in src
    assert "render_sse(" in src
    # The outcome→transport branching moved to the runner/renderer (single home).
    assert "TurnOutcome.BILLING_UNAVAILABLE" not in src
    assert "TurnOutcome.INSUFFICIENT_BALANCE" not in src
    assert "TurnOutcome.HANDOFF" not in src


def test_chat_stream_has_no_inline_handoff_paywall_persistence():
    # AC1: no inline handoff (load+status check), no inline paywall
    # (has_sufficient_balance), no inline persistence (conversations/messages
    # writes), no full_response accumulation in the shell (G5).
    src = _read_chat_stream_function_only()
    # Inline handoff: the old shell read the conversation status inline.
    assert "_load_owned_conversation(" not in src
    assert 'conv_status == "HUMAN_REQUESTED"' not in src
    # Inline paywall: the old shell called has_sufficient_balance directly.
    assert "has_sufficient_balance(" not in src
    assert "BillingCacheUnavailable" not in src
    # Inline persistence: the old shell wrote messages/conversations directly.
    assert 'table("messages")' not in src
    assert 'table("conversations")' not in src
    assert "current_unread" not in src
    # G5: text accumulation moved to the orchestrator (single source of truth).
    assert "full_response" not in src


def test_chat_stream_removed_agent_existence_precheck():
    # AC11/D5: the agent-existence pre-check (AgentService.get_agent_by_id /
    # _load_agent_for_company_or_404 as an unconditional endpoint gate) is gone.
    # The agent is resolved by the core only on PROCEED; CONFIG_REQUIRED renders
    # a friendly SSE. get_agent_by_id must not appear anywhere in chat_stream.
    src = _read_chat_stream_function_only()
    assert "get_agent_by_id(" not in src
    assert "AgentService" not in src
    # Fase 2: the CONFIG_REQUIRED-on-PROCEED friendly message now lives in the
    # sse_renderer (the wire mapping's home). The shell still emits the friendly
    # no-agent copy on the missing-agentId input-validation path (preserved UX).
    assert "Nenhum agente configurado" in src


def test_chat_stream_widget_security_precedes_evaluate_pre_turn():
    # D5/G1/AC11: widget-security runs BEFORE evaluate_pre_turn (so the widget
    # rate-limit applies to handoff/no-balance too). Pin the source ORDER.
    src = _read_chat_stream_function_only()
    widget_idx = src.index("check_widget_rate_limit(")
    pre_turn_idx = src.index("resolve_pre_turn(")
    assert widget_idx < pre_turn_idx


def test_chat_stream_passes_persist_user_message_false():
    # D4: /chat/stream does NOT persist the user message (the frontend writes it
    # and dedups the Realtime echo). The shell forwards persist_user_message=False.
    src = _read_chat_stream_function_only()
    assert "persist_user_message=False" in src


# --------------------------------------------------------------------------- #
# Orchestrator-level render-by-outcome (fakes only; no HTTP/SSE/Redis/LLM).
# These exercise the SAME evaluate_pre_turn the shell calls, proving the
# outcomes the shell renders are produced by the seam (§8.6, AC3/AC4/AC8).
# --------------------------------------------------------------------------- #
class _StubStore:
    """Fake ConversationStore recording writes, for outcome-level assertions."""

    def __init__(self, conversation=None):
        self._conversation = conversation
        self.persist_user_turn_calls: List[Dict[str, Any]] = []
        self.persist_turn_calls: List[Dict[str, Any]] = []

    async def load_owned(self, *, session_id, company_id, **_k):
        return self._conversation

    async def persist_user_turn(self, **kwargs):
        self.persist_user_turn_calls.append(kwargs)

    async def persist_turn(self, **kwargs):
        self.persist_turn_calls.append(kwargs)


class _StubBillingGate:
    def __init__(self, outcome):
        from app.services.chat_turn_orchestrator import TurnOutcome as _TO

        self._outcome = outcome
        self._TO = _TO

    async def evaluate(self, company_id):
        return self._outcome


def _stream_orch(*, store, billing):
    from app.services.chat_turn_orchestrator import ChatTurnOrchestrator
    from app.services.turn_ports.handoff_policy import HandoffPolicy

    return ChatTurnOrchestrator(
        supabase_client=object(),
        qdrant_service=None,
        conversation_store=store,
        handoff_policy=HandoffPolicy(store),
        billing_gate=billing,
    )


def _stream_req(**over):
    from app.services.chat_turn_orchestrator import TurnRequest

    base = dict(
        user_message="hi",
        company_id="c1",
        session_id="s1",
        user_id=None,
        agent_id="a1",
        channel="web",
        persist_user_message=False,
    )
    base.update(over)
    return TurnRequest(**base)


def test_stream_outcome_handoff_persists_user_turn():
    # AC4/D3: HANDOFF now PERSISTS (msg user + unread+1) on /chat/stream — the
    # fix for the old bug where the stream only emitted [HUMAN_MODE]. The shell
    # then emits [HUMAN_MODE]+[DONE] without re-persisting.
    from app.services.chat_turn_orchestrator import TurnOutcome

    store = _StubStore(conversation={"id": "conv-1", "status": "HUMAN_REQUESTED", "unread_count": 2})
    billing = _StubBillingGate(TurnOutcome.PROCEED)
    orch = _stream_orch(store=store, billing=billing)

    pre = asyncio.run(orch.evaluate_pre_turn(_stream_req()))

    assert pre.outcome == TurnOutcome.HANDOFF
    # Persisted exactly once (the fix); paywall NOT consulted (short-circuit).
    assert len(store.persist_user_turn_calls) == 1
    assert store.persist_turn_calls == []


def test_stream_outcome_insufficient_balance_is_dry():
    # AC4: INSUFFICIENT_BALANCE persists NOTHING (dry, anti-abuse). The shell
    # renders a [DONE]-only StreamingResponse.
    from app.services.chat_turn_orchestrator import TurnOutcome

    store = _StubStore(conversation=None)
    billing = _StubBillingGate(TurnOutcome.INSUFFICIENT_BALANCE)
    orch = _stream_orch(store=store, billing=billing)

    pre = asyncio.run(orch.evaluate_pre_turn(_stream_req()))

    assert pre.outcome == TurnOutcome.INSUFFICIENT_BALANCE
    assert store.persist_user_turn_calls == []
    assert store.persist_turn_calls == []


def test_stream_outcome_billing_unavailable_is_dry_and_not_exception():
    # AC3: BILLING_UNAVAILABLE is an OUTCOME (the 503 is a transport decision in
    # the shell), not an exception, and persists nothing.
    from app.services.chat_turn_orchestrator import TurnOutcome

    store = _StubStore(conversation=None)
    billing = _StubBillingGate(TurnOutcome.BILLING_UNAVAILABLE)
    orch = _stream_orch(store=store, billing=billing)

    pre = asyncio.run(orch.evaluate_pre_turn(_stream_req()))

    assert pre.outcome == TurnOutcome.BILLING_UNAVAILABLE
    assert store.persist_user_turn_calls == []
    assert store.persist_turn_calls == []


def test_stream_outcome_proceed_when_open_and_funded():
    # PROCEED path: open conversation + sufficient balance → PROCEED, no writes
    # at the gate (persistence happens post-stream in the orchestrator).
    from app.services.chat_turn_orchestrator import TurnOutcome

    store = _StubStore(conversation={"id": "conv-1", "status": "open", "unread_count": 0})
    billing = _StubBillingGate(TurnOutcome.PROCEED)
    orch = _stream_orch(store=store, billing=billing)

    pre = asyncio.run(orch.evaluate_pre_turn(_stream_req()))

    assert pre.outcome == TurnOutcome.PROCEED
    assert store.persist_user_turn_calls == []


def test_stream_handoff_works_without_agent_row():
    # D5/G1/AC11: HANDOFF must work even when the agent row is absent/deleted —
    # the gate never touches the agent. agent_id resolution is the core's job on
    # PROCEED only. Here a HUMAN_REQUESTED conversation short-circuits to HANDOFF
    # and persists regardless of the agent existing.
    from app.services.chat_turn_orchestrator import TurnOutcome

    store = _StubStore(conversation={"id": "conv-1", "status": "HUMAN_REQUESTED", "unread_count": 0})
    billing = _StubBillingGate(TurnOutcome.PROCEED)
    orch = _stream_orch(store=store, billing=billing)

    # agent_id present but the row does not exist anywhere — gate ignores it.
    pre = asyncio.run(orch.evaluate_pre_turn(_stream_req(agent_id="ghost-agent")))

    assert pre.outcome == TurnOutcome.HANDOFF
    assert len(store.persist_user_turn_calls) == 1


# =========================================================================== #
# PRIORITY 4 — Cache key shape (weak: company:agent:updated_at)
# =========================================================================== #
def _reset_graph_cache():
    lcs._graphs_cache.clear()


def test_cache_key_is_company_agent_updatedat_and_hits_on_repeat():
    _reset_graph_cache()

    created: List[str] = []

    async def _fake_create_agent_graph(**kwargs: Any):
        created.append(kwargs.get("company_id"))
        return types.SimpleNamespace(tag="graph")

    # create_agent_graph is imported locally inside get_or_create_graph
    # (`from app.agents import create_agent_graph`), so patch the package attr.
    _restore_create = _install_create_agent_graph(_fake_create_agent_graph)
    try:
        cfg = {"updated_at": "2026-05-29T00:00:00", "llm_model": "gpt-4o"}

        g1 = asyncio.run(
            get_or_create_graph(
                company_id="c1",
                agent_id="a1",
                agent_config=cfg,
                qdrant_service=None,
                supabase_client=None,
            )
        )
        # Second call with the SAME config -> cache HIT (no new create).
        g2 = asyncio.run(
            get_or_create_graph(
                company_id="c1",
                agent_id="a1",
                agent_config=cfg,
                qdrant_service=None,
                supabase_client=None,
            )
        )
        assert g1 is g2
        assert len(created) == 1  # created exactly once
        # Strong key (D5) preserves the company:agent: prefix.
        assert any(k.startswith("c1:a1:") for k in lcs._graphs_cache)
    finally:
        _restore_create()
        _reset_graph_cache()


def test_cache_key_changes_on_provider_model_tools_delegations():
    # D5 strong key: the fingerprint varies by provider/model/tools/delegations
    # (and updated_at + runtime_version), so changing ANY of those — even with the
    # SAME updated_at — produces a different key and forces a rebuild. This is the
    # opposite of the old weak-key behavior (which only varied by updated_at).
    _reset_graph_cache()
    created: List[Dict[str, Any]] = []

    async def _fake_create_agent_graph(**kwargs: Any):
        created.append(kwargs)
        return types.SimpleNamespace(tag=len(created))

    _restore_create = _install_create_agent_graph(_fake_create_agent_graph)
    try:
        base_ts = "2026-05-29T00:00:00"

        # 1) initial config
        asyncio.run(
            get_or_create_graph(
                company_id="c1", agent_id="a1",
                agent_config={"updated_at": base_ts, "tools": ["a"], "llm_model": "gpt-4o"},
                qdrant_service=None, supabase_client=None,
            )
        )
        # 2) SAME updated_at but DIFFERENT tools/model/delegations/provider -> MISS.
        asyncio.run(
            get_or_create_graph(
                company_id="c1", agent_id="a1",
                agent_config={
                    "updated_at": base_ts,
                    "tools": ["a", "b", "c"],          # changed
                    "delegations": ["sub1"],            # changed
                    "llm_model": "claude-sonnet",       # changed
                    "llm_provider": "anthropic",        # changed
                },
                qdrant_service=None, supabase_client=None,
            )
        )
        assert len(created) == 2  # rebuilt because provider/model/tools/delegations changed
    finally:
        _restore_create()
        _reset_graph_cache()


def test_cache_key_isolates_by_agent_id():
    _reset_graph_cache()
    created: List[Dict[str, Any]] = []

    async def _fake_create_agent_graph(**kwargs: Any):
        created.append(kwargs)
        return types.SimpleNamespace(tag=len(created))

    _restore_create = _install_create_agent_graph(_fake_create_agent_graph)
    try:
        cfg = {"updated_at": "2026-05-29T00:00:00", "llm_model": "gpt-4o"}
        asyncio.run(get_or_create_graph(company_id="c1", agent_id="a1", agent_config=cfg, qdrant_service=None, supabase_client=None))
        asyncio.run(get_or_create_graph(company_id="c1", agent_id="a2", agent_config=cfg, qdrant_service=None, supabase_client=None))
        assert len(created) == 2  # no cross-agent bleed
        assert any(k.startswith("c1:a1:") for k in lcs._graphs_cache)
        assert any(k.startswith("c1:a2:") for k in lcs._graphs_cache)
    finally:
        _restore_create()
        _reset_graph_cache()


def test_invalidate_by_prefix_clears_all_versions_for_agent():
    _reset_graph_cache()
    lcs._graphs_cache["c1:a1:2026-05-29T00:00:00"] = object()
    lcs._graphs_cache["c1:a1:2026-05-30T00:00:00"] = object()
    lcs._graphs_cache["c1:a2:2026-05-29T00:00:00"] = object()

    lcs.invalidate_agent_graph_cache("c1", "a1")

    keys = list(lcs._graphs_cache.keys())
    assert "c1:a2:2026-05-29T00:00:00" in keys  # untouched
    assert all(not k.startswith("c1:a1:") for k in keys)  # all a1 versions gone
    _reset_graph_cache()


def test_compute_graph_cache_key_is_pure_and_strong():
    # D5: compute_graph_cache_key must be a pure function over the in-memory agent
    # dict — NO supabase client, NO DB calls — and must produce a strong key that
    # varies by provider/model/runtime_version while staying deterministic.
    agent = {
        "id": "a1",
        "updated_at": "2026-05-29T00:00:00",
        "llm_provider": "openai",
        "llm_model": "gpt-4o",
        "tools": ["a"],
        "delegations": ["sub1"],
    }

    # (1) pure/deterministic + (4) identical input -> identical key.
    k1 = compute_graph_cache_key("c1", "a1", agent)
    k2 = compute_graph_cache_key("c1", "a1", dict(agent))
    assert k1 == k2

    # (2) key starts with company:agent: prefix.
    assert k1.startswith("c1:a1:")

    # (3) changing llm_provider changes the digest.
    assert compute_graph_cache_key("c1", "a1", {**agent, "llm_provider": "anthropic"}) != k1
    # (3) changing llm_model changes the digest.
    assert compute_graph_cache_key("c1", "a1", {**agent, "llm_model": "claude-sonnet"}) != k1
    # (3) changing runtime_version changes the digest.
    bumped = compute_graph_cache_key(
        "c1", "a1", agent, runtime_version=str(int(RUNTIME_GRAPH_VERSION) + 1)
    )
    assert bumped != k1


# =========================================================================== #
# PRIORITY 5 — Query-count baseline for agent/graph resolution
# =========================================================================== #
def test_query_count_baseline_for_agent_graph_resolution():
    # Baseline for the Sprint 4 "zero extra queries" gate. Pins how many DB
    # reads happen during a single aggregated turn's resolution today:
    #   - supabase.get_company(...)        -> 1
    #   - _get_raw_agent: agents table .execute() -> 1
    #   - get_or_create_graph: cache key derived from agent dict ONLY (0 queries)
    #   - history: conversation_history NOT provided -> get_conversation_history 1
    _RecordingGuardrail.received = []
    company = {"id": "company-1"}
    agent = {
        "id": "agent-1",
        "llm_provider": "openai",
        "llm_model": "gpt-4o",
        "security_settings": {"enabled": True},
        "updated_at": "2026-05-29T00:00:00",
    }
    supabase = FakeSupabase(company=company, agent=agent)
    svc = _make_service()
    svc.supabase = supabase
    svc.qdrant = None

    lcs._graphs_cache.clear()
    _restore_api_key = _install_api_key_resolver()
    _restore_gr = _install_guardrail_cls(_RecordingGuardrail)

    goc_calls: List[Dict[str, Any]] = []

    async def _fake_create(**kwargs: Any):
        # Each MISS in get_or_create_graph builds exactly one graph.
        goc_calls.append(kwargs)
        return object()

    _restore_create = _install_create_agent_graph(_fake_create)
    captured, _restore_invoke = _install_invoke_agent(
        {"response": "ok", "tokens_total": 5}
    )
    try:
        asyncio.run(
            svc.process_message(
                user_message="hello",
                company_id="company-1",
                user_id="user-1",
                session_id="sess-1",
                agent_id="agent-1",
                # conversation_history omitted -> triggers get_conversation_history
            )
        )
    finally:
        _restore_api_key()
        _restore_gr()
        _restore_create()
        _restore_invoke()
        lcs._graphs_cache.clear()

    # BASELINE (pin these numbers; Sprint 4 must not exceed them):
    assert supabase.get_company_count == 1
    assert supabase.query_count == 1        # exactly one agents-table read
    assert supabase.table_calls == ["agents"]
    assert supabase.history_count == 1      # one history fetch
    assert len(goc_calls) == 1              # graph acquired once
    # Graph cache key derivation consumed NO extra DB query (it reads agent dict).
    # Total DB-ish reads during resolution today == 3.
    total_reads = supabase.get_company_count + supabase.query_count + supabase.history_count
    assert total_reads == 3


def test_query_count_baseline_skips_history_when_provided():
    # When conversation_history is provided, no history query runs (pin it).
    company = {"id": "company-1"}
    agent = {
        "id": "agent-1",
        "llm_provider": "openai",
        "llm_model": "gpt-4o",
        "security_settings": {"enabled": True},
        "updated_at": "2026-05-29T00:00:00",
    }
    supabase = FakeSupabase(company=company, agent=agent)
    svc = _make_service()
    svc.supabase = supabase
    svc.qdrant = None

    lcs._graphs_cache.clear()
    _restore_api_key = _install_api_key_resolver()
    _restore_gr = _install_guardrail_cls(_RecordingGuardrail)

    async def _fake_create(**_k: Any):
        return object()

    _restore_create = _install_create_agent_graph(_fake_create)
    captured, _restore_invoke = _install_invoke_agent(
        {"response": "ok", "tokens_total": 1}
    )
    try:
        asyncio.run(
            svc.process_message(
                user_message="hello",
                company_id="company-1",
                user_id="user-1",
                session_id="sess-1",
                agent_id="agent-1",
                conversation_history=[{"role": "user", "content": "prev"}],
            )
        )
    finally:
        _restore_api_key()
        _restore_gr()
        _restore_create()
        _restore_invoke()
        lcs._graphs_cache.clear()

    assert supabase.get_company_count == 1
    assert supabase.query_count == 1
    assert supabase.history_count == 0  # provided history => no fetch


# =========================================================================== #
# BONUS — blocked path metrics contract (compat for the thin shell, SPEC §5.1.1)
# =========================================================================== #
def test_aggregated_blocked_path_returns_block_reason_with_metrics():
    company = {"id": "company-1"}
    agent = {
        "id": "agent-1",
        "llm_provider": "openai",
        "llm_model": "gpt-4o",
        "security_settings": {"enabled": True},
        "updated_at": "2026-05-29T00:00:00",
    }
    supabase = FakeSupabase(company=company, agent=agent)
    svc = _make_service()
    svc.supabase = supabase

    class _BlockingGuardrail:
        def __init__(self, agent_config, company_id):
            self.fail_close = True

        async def validate_input(self, text: str):
            return True, "BLOCKED_REASON", text

    _restore_api_key = _install_api_key_resolver()
    _restore_gr = _install_guardrail_cls(_BlockingGuardrail)
    try:
        response, metrics = asyncio.run(
            svc.process_message(
                user_message="bad",
                company_id="company-1",
                user_id="user-1",
                session_id="sess-1",
                agent_id="agent-1",
            )
        )
    finally:
        _restore_gr()
        _restore_api_key()

    # Block short-circuits: response == block_reason, metrics present + ended.
    assert response == "BLOCKED_REASON"
    assert metrics is not None
    assert metrics.end_time is not None


def test_aggregated_collect_metrics_false_returns_none_metrics_on_block():
    company = {"id": "company-1"}
    agent = {
        "id": "agent-1",
        "llm_provider": "openai",
        "llm_model": "gpt-4o",
        "security_settings": {"enabled": True},
        "updated_at": "2026-05-29T00:00:00",
    }
    supabase = FakeSupabase(company=company, agent=agent)
    svc = _make_service()
    svc.supabase = supabase

    class _BlockingGuardrail:
        def __init__(self, agent_config, company_id):
            self.fail_close = True

        async def validate_input(self, text: str):
            return True, "BLOCKED_REASON", text

    _restore_api_key = _install_api_key_resolver()
    _restore_gr = _install_guardrail_cls(_BlockingGuardrail)
    try:
        response, metrics = asyncio.run(
            svc.process_message(
                user_message="bad",
                company_id="company-1",
                user_id="user-1",
                session_id="sess-1",
                agent_id="agent-1",
                collect_metrics=False,
            )
        )
    finally:
        _restore_gr()
        _restore_api_key()

    assert response == "BLOCKED_REASON"
    assert metrics is None  # collect_metrics=False => None end-to-end


# =========================================================================== #
# NOTE (Fase 4b / PR-4): the D9 facade regression tests (webhook sync->async
# proxy + module-level conversation_store) were removed together with the
# proxy itself. ConversationStore against the REAL AsyncSupabaseClient shape
# is covered by tests/services/test_conversation_store.py; the WhatsApp turn
# pipeline is covered by tests/services/test_whatsapp_turn_service.py.
# =========================================================================== #


# =========================================================================== #
# SPRINT 5 (Fase 3) — /chat wired as a THIN shell (evaluate_pre_turn + run_turn).
# Source-asserts scoped to `chat_endpoint` + orchestrator-level render-by-outcome
# (fakes only; no HTTP/Redis/LLM — AC9). We pin that handoff/paywall/persistence
# inline AND `_load_owned_conversation` AND the unread read-modify-write are GONE,
# and that the single pre-turn gate drives /chat (§8.6, AC1/AC3/AC5/AC12).
# =========================================================================== #
def _read_chat_endpoint_function_only() -> str:
    """Slice ONLY the `chat_endpoint` function body (up to the next top-level def).

    Mirrors `_read_chat_stream_function_only`: we read chat.py from disk (the
    sibling test packages seed STUB `app.services` modules that break importing
    app.api.chat) and cut at the next decorator / column-0 def after the header.
    """
    import pathlib
    import re

    chat_path = pathlib.Path(lcs.__file__).resolve().parents[1] / "api" / "chat.py"
    full = chat_path.read_text(encoding="utf-8")
    start = full.index("async def chat_endpoint")
    src = full[start:]
    body_start = src.index("\n") + 1
    m = re.search(r"\n(@router\.|def |async def )", src[body_start:])
    if m:
        return src[: body_start + m.start()]
    return src


def test_chat_endpoint_calls_evaluate_pre_turn_as_single_gate():
    # AC1/AC2 (Fase 2): /chat delegates handoff+paywall to the TurnRunner seam
    # (build_http_turn_runner + resolve_pre_turn — one gate) and renders the
    # neutral event via render_json, instead of branching on TurnOutcome inline.
    src = _read_chat_endpoint_function_only()
    assert "build_http_turn_runner(" in src
    assert "resolve_pre_turn(" in src
    assert "render_json(" in src
    # The outcome→transport branching moved to the runner/renderer (single home).
    assert "TurnOutcome.BILLING_UNAVAILABLE" not in src
    assert "TurnOutcome.INSUFFICIENT_BALANCE" not in src
    assert "TurnOutcome.HANDOFF" not in src


def test_chat_endpoint_has_no_inline_handoff_paywall_persistence():
    # AC1/AC5: no inline handoff (status check + unread RMW), no inline paywall
    # (has_sufficient_balance), no inline persistence (conversations/messages
    # writes), and no _load_owned_conversation in the endpoint.
    src = _read_chat_endpoint_function_only()
    # Ownership helper removed (migrated to ConversationStore).
    assert "_load_owned_conversation(" not in src
    # Inline handoff status branch gone.
    assert 'conv_status == "HUMAN_REQUESTED"' not in src
    # Inline paywall gone.
    assert "has_sufficient_balance(" not in src
    assert "BillingCacheUnavailable" not in src
    # Inline persistence gone (no direct table writes in the endpoint).
    assert 'table("messages")' not in src
    assert 'table("conversations")' not in src
    # Unread read-modify-write gone (:413/:527).
    assert "current_unread" not in src
    assert "unread_count" not in src


def test_chat_endpoint_helper_load_owned_conversation_removed_from_module():
    # AC5: `_load_owned_conversation` is removed from chat.py entirely (ownership
    # now lives in the ConversationStore). The whole module must not define it.
    import pathlib

    chat_path = pathlib.Path(lcs.__file__).resolve().parents[1] / "api" / "chat.py"
    full = chat_path.read_text(encoding="utf-8")
    assert "def _load_owned_conversation" not in full


def test_chat_endpoint_passes_persist_user_message_true():
    # D4: /chat IS the user-message writer (the legacy endpoint persisted the
    # user message on success). The shell forwards persist_user_message=True.
    src = _read_chat_endpoint_function_only()
    assert "persist_user_message=True" in src


def test_chat_endpoint_preserves_config_required_friendly_message():
    # AC1/AC11: CONFIG_REQUIRED (agent absent from the core) maps to the existing
    # friendly message, preserving the legacy UX.
    src = _read_chat_endpoint_function_only()
    assert "CONFIG_REQUIRED" in src
    assert "Nenhum agente configurado" in src


# --------------------------------------------------------------------------- #
# Orchestrator-level render-by-outcome for /chat (persist_user_message=True).
# These exercise the SAME evaluate_pre_turn the /chat shell calls, proving the
# outcomes are produced by the seam and that persistence is asymmetric (§8.6).
# --------------------------------------------------------------------------- #
def _chat_req(**over):
    # /chat forwards persist_user_message=True (D4).
    over.setdefault("persist_user_message", True)
    return _stream_req(**over)


def test_chat_outcome_handoff_persists_user_turn_and_paywall_not_consulted():
    # AC3/AC4/D3: HANDOFF persists (msg user + unread+1) via the store; the
    # paywall is NOT consulted (short-circuit). The shell renders output="".
    from app.services.chat_turn_orchestrator import TurnOutcome

    store = _StubStore(conversation={"id": "conv-1", "status": "HUMAN_REQUESTED", "unread_count": 2})
    billing = _StubBillingGate(TurnOutcome.PROCEED)
    orch = _stream_orch(store=store, billing=billing)

    pre = asyncio.run(orch.evaluate_pre_turn(_chat_req()))

    assert pre.outcome == TurnOutcome.HANDOFF
    assert len(store.persist_user_turn_calls) == 1
    assert store.persist_turn_calls == []


def test_chat_outcome_insufficient_balance_is_dry():
    # AC3/AC4: INSUFFICIENT_BALANCE persists NOTHING (dry). The shell renders
    # output="" (no error) to avoid a frontend "connection error".
    from app.services.chat_turn_orchestrator import TurnOutcome

    store = _StubStore(conversation=None)
    billing = _StubBillingGate(TurnOutcome.INSUFFICIENT_BALANCE)
    orch = _stream_orch(store=store, billing=billing)

    pre = asyncio.run(orch.evaluate_pre_turn(_chat_req()))

    assert pre.outcome == TurnOutcome.INSUFFICIENT_BALANCE
    assert store.persist_user_turn_calls == []
    assert store.persist_turn_calls == []


def test_chat_outcome_billing_unavailable_is_outcome_not_exception():
    # AC3: BILLING_UNAVAILABLE is an OUTCOME (the 503 is a transport decision in
    # the /chat shell), not an exception, and persists nothing.
    from app.services.chat_turn_orchestrator import TurnOutcome

    store = _StubStore(conversation=None)
    billing = _StubBillingGate(TurnOutcome.BILLING_UNAVAILABLE)
    orch = _stream_orch(store=store, billing=billing)

    pre = asyncio.run(orch.evaluate_pre_turn(_chat_req()))

    assert pre.outcome == TurnOutcome.BILLING_UNAVAILABLE
    assert store.persist_user_turn_calls == []
    assert store.persist_turn_calls == []


def test_chat_outcome_proceed_reuses_cached_conversation_zero_reload():
    # AC12/D6: the conversation loaded in evaluate_pre_turn is cached on the
    # instance and reused by persist_turn — zero re-load on the happy path.
    from app.services.chat_turn_orchestrator import TurnOutcome

    conv = {"id": "conv-1", "status": "open", "unread_count": 0}
    store = _StubStore(conversation=conv)
    billing = _StubBillingGate(TurnOutcome.PROCEED)
    orch = _stream_orch(store=store, billing=billing)

    pre = asyncio.run(orch.evaluate_pre_turn(_chat_req()))

    assert pre.outcome == TurnOutcome.PROCEED
    # D6/G2: the loaded conversation is cached on the instance for persist reuse.
    assert orch._pre_turn_conversation is conv
    assert pre.conversation is conv
    # The gate does not persist on PROCEED (persistence happens post-run_turn).
    assert store.persist_user_turn_calls == []


def test_chat_outcome_cross_tenant_raises_domain_error_for_404_mapping():
    # AC5: cross-tenant ownership → CrossTenantConversationError (shell maps 404).
    # The core never raises HTTPException; it raises the domain error.
    from app.services.chat_turn_orchestrator import ChatTurnOrchestrator
    from app.services.turn_ports.conversation_store import (
        CrossTenantConversationError,
    )
    from app.services.turn_ports.handoff_policy import HandoffPolicy

    class _CrossTenantStore:
        async def load_owned(self, *, session_id, company_id, **_k):
            raise CrossTenantConversationError("cross-tenant")

    store = _CrossTenantStore()
    orch = ChatTurnOrchestrator(
        supabase_client=object(),
        qdrant_service=None,
        conversation_store=store,
        handoff_policy=HandoffPolicy(store),
        billing_gate=_StubBillingGate(None),
    )

    raised = None
    try:
        asyncio.run(orch.evaluate_pre_turn(_chat_req()))
    except CrossTenantConversationError as exc:
        raised = exc
    except Exception as exc:  # pragma: no cover - defensive
        raised = exc

    assert isinstance(raised, CrossTenantConversationError)


def test_chat_outcome_ownership_unavailable_raises_domain_error_for_503_mapping():
    # AC5: ownership verification failure → ConversationOwnershipUnavailable
    # (shell maps 503). Domain error, not HTTPException, from the core.
    from app.services.chat_turn_orchestrator import ChatTurnOrchestrator
    from app.services.turn_ports.conversation_store import (
        ConversationOwnershipUnavailable,
    )
    from app.services.turn_ports.handoff_policy import HandoffPolicy

    class _UnavailableStore:
        async def load_owned(self, *, session_id, company_id, **_k):
            raise ConversationOwnershipUnavailable("ownership check failed")

    store = _UnavailableStore()
    orch = ChatTurnOrchestrator(
        supabase_client=object(),
        qdrant_service=None,
        conversation_store=store,
        handoff_policy=HandoffPolicy(store),
        billing_gate=_StubBillingGate(None),
    )

    raised = None
    try:
        asyncio.run(orch.evaluate_pre_turn(_chat_req()))
    except ConversationOwnershipUnavailable as exc:
        raised = exc
    except Exception as exc:  # pragma: no cover - defensive
        raised = exc

    assert isinstance(raised, ConversationOwnershipUnavailable)
