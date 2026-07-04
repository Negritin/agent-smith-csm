"""
Sprint 3 — `stream_agent` is now a PURE iterator (SPEC 20260529_172113-738e71, D4).

Before Sprint 3 the streaming adapter (`app.agents.graph.stream_agent`) wrapped
its event loop in a retry loop AND swallowed any final exception into a text
token (`\n\n[Erro interno no servidor durante a geração da resposta. ...]`).
That swallowing made it IMPOSSIBLE for the orchestrator's `_with_recovery`
policy to ever see a recoverable pool/connection/SSL error.

This test proves the new contract: a recoverable error raised by
`graph.astream_events(...)` PROPAGATES out of `stream_agent` as an exception and
is NOT emitted as an `[Erro interno...]` text token.

Conventions (mirror tests/services/test_chat_turn_orchestrator.py):
  - NO pytest-asyncio; async driven with asyncio.run(...).
  - Plain asserts; fakes injected.
  - Env seeded by tests/services/conftest.py BEFORE importing app.*.
  - NO external service touched: `_build_initial_state` is monkeypatched to a
    minimal stub so the heavy graph-build path is never reached, and LangSmith is
    forced off.
"""

from __future__ import annotations

import asyncio
from typing import Any, List

import pytest

import app.agents.graph as graph_mod
from app.agents.nodes import PromptSafetyError

try:
    from psycopg import OperationalError
except ImportError:  # pragma: no cover - fallback if psycopg absent
    class OperationalError(Exception):
        pass


class _RaisingGraph:
    """Fake LangGraph whose astream_events raises a recoverable connection error
    on first iteration (before any token is streamed)."""

    def astream_events(self, *_a: Any, **_k: Any):
        async def _gen():
            # An OperationalError whose message also matches the recoverable
            # keyword set ("connection closed") — recoverable by every detector.
            raise OperationalError("server closed the connection unexpectedly")
            yield  # pragma: no cover - unreachable, makes this an async generator

        return _gen()


class _RaisingPromptSafetyGraph:
    """Fake LangGraph whose astream_events raises PromptSafetyError on the first
    iteration. The adapter must let it PROPAGATE (the orchestrator maps it to the
    `blocked` channel); it must NOT be swallowed into a text token."""

    def __init__(self, *, after_token: bool = False) -> None:
        self._after_token = after_token

    def astream_events(self, *_a: Any, **_k: Any):
        async def _gen():
            if self._after_token:
                # Emit one real v2 token first, THEN raise — proves the raise
                # propagates even mid-stream (not converted to a text token).
                yield _v2_chat_model_stream("blocked? ")
            raise PromptSafetyError("prompt blocked by safety policy")
            yield  # pragma: no cover - unreachable, makes this an async generator

        return _gen()


def _v2_chat_model_stream(text: str, *, node: str = "agent") -> dict:
    """Build an `on_chat_model_stream` event in the langchain-core **v2** shape:
    `data["chunk"]` is an `AIMessageChunk` and `metadata["langgraph_node"]`
    carries the emitting LangGraph node (propagated by LangGraph in v1 and v2)."""
    from langchain_core.messages import AIMessageChunk

    return {
        "event": "on_chat_model_stream",
        "name": "ChatModel",
        "metadata": {"langgraph_node": node},
        "data": {"chunk": AIMessageChunk(content=text)},
    }


def _v2_chain_event(kind: str, name: str, data: dict | None = None) -> dict:
    """Build an `on_chain_start`/`on_chain_end` node-lifecycle event (v2 shape):
    LangGraph names the node event by the node name in both v1 and v2."""
    return {"event": kind, "name": name, "data": data or {}}


class _V2EventGraph:
    """Fake LangGraph that replays a fixed sequence of **v2-format** events so the
    parsing in `stream_agent` can be asserted without the real framework.

    The sequence exercises: token streaming from the `agent` node, the tools-node
    lifecycle (start emits tool_start dicts from the last AIMessage's tool_calls;
    end emits tool_end), and the `agent` end fallback when nothing streamed."""

    def __init__(self, events: List[dict]) -> None:
        self._events = events

    def astream_events(self, *_a: Any, **_k: Any):
        async def _gen():
            for ev in self._events:
                yield ev

        return _gen()


def _patch_build_initial_state(monkey):
    """Replace _build_initial_state with a trivial async stub so stream_agent
    reaches the astream_events loop without the heavy production build path."""
    orig = graph_mod._build_initial_state

    async def _fake_build(*_a: Any, **_k: Any):
        # (initial_state, config, real_agent_data)
        return {"messages": []}, {"configurable": {}}, {"id": "agent-1"}

    graph_mod._build_initial_state = _fake_build  # type: ignore[assignment]

    def _restore():
        graph_mod._build_initial_state = orig  # type: ignore[assignment]

    return _restore


def _force_langsmith_off():
    orig = graph_mod.is_langsmith_enabled if hasattr(graph_mod, "is_langsmith_enabled") else None
    # is_langsmith_enabled is imported lazily inside stream_agent from
    # app.core.langsmith_setup, so patch there.
    import app.core.langsmith_setup as ls

    orig_fn = ls.is_langsmith_enabled
    ls.is_langsmith_enabled = lambda: False  # type: ignore[assignment]

    def _restore():
        ls.is_langsmith_enabled = orig_fn  # type: ignore[assignment]

    return _restore


async def _drain(gen) -> List[str]:
    out: List[str] = []
    async for tok in gen:
        out.append(tok)
    return out


def test_stream_agent_propagates_recoverable_error_no_swallow_token():
    restore_build = _patch_build_initial_state(None)
    restore_ls = _force_langsmith_off()
    try:
        gen = graph_mod.stream_agent(
            graph=_RaisingGraph(),
            user_message="hello",
            company_id="company-1",
            user_id="user-1",
            session_id="sess-1",
            company_config={"id": "agent-1"},
            options=None,
            supabase_client=None,
            agent_id="agent-1",
            async_supabase_client=None,
        )

        raised = False
        tokens: List[str] = []
        try:
            tokens = asyncio.run(_drain(gen))
        except OperationalError:
            raised = True
        except Exception as e:  # noqa: BLE001 — any recoverable connection error counts
            # Some environments may surface a bare Exception with the keyword text.
            raised = "connection" in str(e).lower() or "closed" in str(e).lower()

        # The error must PROPAGATE (not be swallowed) ...
        assert raised, f"stream_agent swallowed the error; tokens={tokens!r}"
        # ... and NO [Erro interno...] text token must have been yielded.
        assert not any("[Erro interno no servidor" in t for t in tokens)
    finally:
        restore_ls()
        restore_build()


def _run_stream(graph) -> List[Any]:
    """Drive `stream_agent` over `graph` with the standard test scaffolding
    (build path stubbed, LangSmith off, no DB clients so the memory trigger is
    skipped) and return every item it yields (strings AND status dicts)."""
    restore_build = _patch_build_initial_state(None)
    restore_ls = _force_langsmith_off()
    try:
        gen = graph_mod.stream_agent(
            graph=graph,
            user_message="hello",
            company_id="company-1",
            user_id="user-1",
            session_id="sess-1",
            company_config={"id": "agent-1"},
            options=None,
            supabase_client=None,
            agent_id="agent-1",
            async_supabase_client=None,
        )
        return asyncio.run(_drain(gen))
    finally:
        restore_ls()
        restore_build()


def test_stream_agent_parses_v2_event_schema_tokens_tools_and_fallback():
    """F26 (G7-R2/R3): feed a langchain-core **v2** event sequence and assert
    stream_agent (a) yields token-by-token text from the `agent` node,
    (b) yields tool_start/tool_end status dicts around the `tools` node, and
    (c) uses the `agent` end-fallback only when nothing has streamed yet."""
    from langchain_core.messages import AIMessage

    # AIMessage carrying the tool_calls that the tools node received as input —
    # stream_agent extracts the tool name(s) from the LAST message in
    # data["input"]["messages"].
    ai_with_tool = AIMessage(
        content="",
        tool_calls=[{"name": "knowledge_base_search", "args": {}, "id": "call-1"}],
    )

    events = [
        # Two text chunks from the agent node => token-by-token streaming.
        _v2_chat_model_stream("Hello"),
        _v2_chat_model_stream(", world"),
        # A chunk from a non-agent node must be ignored (subagent runs in tools).
        _v2_chat_model_stream("IGNORED", node="tools"),
        # Tools node lifecycle: start (emits tool_start) then end (emits tool_end).
        _v2_chain_event(
            "on_chain_start",
            "tools",
            {"input": {"messages": [ai_with_tool]}},
        ),
        _v2_chain_event("on_chain_end", "tools"),
        # agent end AFTER text already streamed => fallback must NOT fire again.
        _v2_chain_event(
            "on_chain_end",
            "agent",
            {"output": {"messages": [AIMessage(content="SHOULD-NOT-APPEAR")]}},
        ),
    ]

    out = _run_stream(_V2EventGraph(events))

    texts = [item for item in out if isinstance(item, str)]
    dicts = [item for item in out if isinstance(item, dict)]

    # (a) token-by-token text from the agent node (>=2 chunks), in order.
    assert texts == ["Hello", ", world"], texts
    assert "IGNORED" not in "".join(texts)
    # (c) fallback did not double-emit because has_streamed was already True.
    assert "SHOULD-NOT-APPEAR" not in "".join(texts)

    # (b) tool_start (with classified kind) then tool_end status dicts.
    assert {"event": "tool_start", "name": "knowledge_base_search", "kind": "rag"} in dicts
    assert {"event": "tool_end", "name": "tools", "kind": "tool"} in dicts
    start_idx = next(i for i, d in enumerate(dicts) if d["event"] == "tool_start")
    end_idx = next(i for i, d in enumerate(dicts) if d["event"] == "tool_end")
    assert start_idx < end_idx


def test_stream_agent_v2_agent_end_fallback_when_nothing_streamed():
    """F26 (G7-R3): when NO token streamed (has_streamed False), the
    `on_chain_end`/name=='agent' fallback yields the final message content."""
    from langchain_core.messages import AIMessage

    events = [
        # No on_chat_model_stream at all => has_streamed stays False.
        _v2_chain_event(
            "on_chain_end",
            "agent",
            {"output": {"messages": [AIMessage(content="final answer")]}},
        ),
    ]

    out = _run_stream(_V2EventGraph(events))
    texts = [item for item in out if isinstance(item, str)]

    assert texts == ["final answer"], texts


def test_stream_agent_propagates_prompt_safety_error_no_swallow_token():
    """F26 (G7-R4): a PromptSafetyError raised by the underlying v2 iterator must
    PROPAGATE out of stream_agent (orchestrator -> `blocked`), never converted to
    a text token or swallowed."""
    out: List[Any] = []
    with pytest.raises(PromptSafetyError):
        out = _run_stream(_RaisingPromptSafetyGraph())

    assert not any(isinstance(t, str) and "[Erro interno no servidor" in t for t in out)


def test_stream_agent_prompt_safety_error_propagates_even_after_a_token():
    """F26 (G7-R4): even if a real v2 token was already streamed, a subsequent
    PromptSafetyError still propagates (mid-stream) and is not swallowed."""
    out: List[Any] = []
    with pytest.raises(PromptSafetyError):
        out = _run_stream(_RaisingPromptSafetyGraph(after_token=True))

    # Whatever was emitted before the raise must be real text, never an error token.
    assert not any(isinstance(t, str) and "[Erro interno no servidor" in t for t in out)
