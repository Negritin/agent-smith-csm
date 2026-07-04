"""
Shell tests — MemoryService ASYNC methods OQ-7 parse-failure warnings (M2a).

The 111 goldens in test_memory_core_golden.py pin RETURN VALUES, so the additive
OQ-7 warnings (emitted when a non-empty LLM response parses to an empty result)
have no golden coverage. These tests cover exactly those warnings: each async
method must log a WARNING when the FakeLLM returns non-empty content that the
core parser collapses to empty, and must NOT warn when it parses to a non-empty
result.

House convention (mirrors test_memory_core_golden.py): dummy env seeded BEFORE
importing app.*, asyncio.run for async calls, a FakeLLM, plain asserts, no
pytest-asyncio. No external service is touched (Supabase + LLM are fakes).
"""

from __future__ import annotations

import logging
import os

# app.core.config instantiates Settings() eagerly at import time and needs a
# minimal set of env vars. Seed dummies BEFORE importing app.*.
for _k, _v in {
    "SUPABASE_URL": "https://test.supabase.co",
    "SUPABASE_KEY": "test-key",
    "OPENAI_API_KEY": "sk-test",
    "MINIO_ROOT_USER": "minio",
    "MINIO_ROOT_PASSWORD": "minio123",
    "INTERNAL_JWT_SECRET": "0" * 64,
    "ENCRYPTION_KEY": "SlEoEOtyl89plqTxYcRD-B_9hIBad3-n1rr9isP442Y=",
}.items():
    os.environ.setdefault(_k, _v)

import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from app.services import memory_service as ms  # noqa: E402
from app.services.memory_service import MemoryService  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #
class _FakeLLM:
    """Records the prompt passed to ainvoke and returns a canned content."""

    def __init__(self, content: str = "[]"):
        self._content = content
        self.captured_prompt: str | None = None

    async def ainvoke(self, prompt):
        self.captured_prompt = prompt
        return SimpleNamespace(content=self._content)


class _Msg:
    """Minimal LangChain-message stand-in: has .type and .content."""

    def __init__(self, type_: str, content: str):
        self.type = type_
        self.content = content


def _service() -> MemoryService:
    return MemoryService(supabase_client=object())


def _run(coro):
    return asyncio.run(coro)


SAMPLE_MESSAGES = [
    _Msg("human", "Oi, sou o Breno"),
    _Msg("ai", "Olá Breno!"),
]

WARN_EXTRACT = (
    "[Memory] Async fact extraction returned empty from non-empty LLM "
    "response (possible parse failure)"
)
WARN_SUMMARY = (
    "[Memory] Async session summary returned None from non-empty LLM "
    "response (possible parse failure)"
)
WARN_CONSOLIDATE = (
    "[Memory] Async consolidation returned empty from non-empty LLM "
    "response (possible parse failure)"
)


def _warnings(caplog) -> list[str]:
    return [
        r.message for r in caplog.records if r.levelno == logging.WARNING
    ]


# --------------------------------------------------------------------------- #
# extract_user_facts
# --------------------------------------------------------------------------- #
def test_extract_facts_warns_on_nonempty_response_parsing_empty(caplog):
    """Non-empty content ('not json') -> parser yields [] -> WARNING fired."""
    svc = _service()
    llm = _FakeLLM(content="not json")
    with caplog.at_level(logging.WARNING, logger=ms.logger.name):
        out = _run(svc.extract_user_facts(SAMPLE_MESSAGES, llm=llm))
    assert out == []
    assert WARN_EXTRACT in _warnings(caplog)


def test_extract_facts_no_warn_on_valid_nonempty_list(caplog):
    """Valid non-empty JSON list -> facts non-empty -> NO warning."""
    svc = _service()
    llm = _FakeLLM(content='["fato 1", "fato 2"]')
    with caplog.at_level(logging.WARNING, logger=ms.logger.name):
        out = _run(svc.extract_user_facts(SAMPLE_MESSAGES, llm=llm))
    assert out == ["fato 1", "fato 2"]
    assert WARN_EXTRACT not in _warnings(caplog)


# --------------------------------------------------------------------------- #
# generate_session_summary
# --------------------------------------------------------------------------- #
def test_session_summary_warns_on_nonempty_response_parsing_none(caplog):
    """Non-empty content that is not valid JSON -> None -> WARNING fired."""
    svc = _service()
    llm = _FakeLLM(content="definitely not json")
    with caplog.at_level(logging.WARNING, logger=ms.logger.name):
        out = _run(svc.generate_session_summary(SAMPLE_MESSAGES, llm=llm))
    assert out is None
    assert WARN_SUMMARY in _warnings(caplog)


def test_session_summary_no_warn_on_valid_dict(caplog):
    """Valid summary JSON -> dict result -> NO warning."""
    svc = _service()
    llm = _FakeLLM(
        content=(
            '{"summary": "resumo", "topics": ["t1"], '
            '"decisions": [], "pending_items": ["p1"]}'
        )
    )
    with caplog.at_level(logging.WARNING, logger=ms.logger.name):
        out = _run(svc.generate_session_summary(SAMPLE_MESSAGES, llm=llm))
    assert out == {
        "summary": "resumo",
        "topics": ["t1"],
        "decisions": [],
        "pending_items": ["p1"],
    }
    assert WARN_SUMMARY not in _warnings(caplog)


# --------------------------------------------------------------------------- #
# _consolidate_facts (drives the isinstance(list) branch to empty)
# --------------------------------------------------------------------------- #
def test_consolidate_warns_on_list_of_empties(caplog):
    """A valid JSON list of empty/blank strings sanitizes to [] -> WARNING.

    Both current_facts and new_facts are non-empty so we pass the two early
    returns and reach the isinstance(list) branch; sanitize_facts drops the
    blanks, leaving an empty result -> warning fired.
    """
    svc = _service()
    llm = _FakeLLM(content='["", "   "]')
    with caplog.at_level(logging.WARNING, logger=ms.logger.name):
        out = _run(
            svc._consolidate_facts(["antigo"], ["novo"], llm)
        )
    assert out == []
    assert WARN_CONSOLIDATE in _warnings(caplog)


def test_consolidate_no_warn_on_valid_nonempty_list(caplog):
    """Valid non-empty list sanitizes to non-empty -> NO warning."""
    svc = _service()
    llm = _FakeLLM(content='["fato consolidado"]')
    with caplog.at_level(logging.WARNING, logger=ms.logger.name):
        out = _run(
            svc._consolidate_facts(["antigo"], ["novo"], llm)
        )
    assert out == ["fato consolidado"]
    assert WARN_CONSOLIDATE not in _warnings(caplog)


# --------------------------------------------------------------------------- #
# M2b intentional diff — the FOUR `[:8]` borders + the sanitize path of
# _consolidate_facts now honor MEMORY_MAX_FACTS_PER_USER instead of a
# hardcoded 8. We pin a value != 8 (2) and prove every exit path truncates to
# it. Before M2b these four borders returned `[:8]` and would NOT respect the
# config knob on the async (hot) path — this is the bug this sprint fixes.
# --------------------------------------------------------------------------- #
class _RaisingLLM:
    """ainvoke always raises -> drives the exception fallback border."""

    async def ainvoke(self, prompt):
        raise RuntimeError("boom")


def test_consolidate_borders_respect_max_facts_constant(monkeypatch):
    monkeypatch.setattr(ms, "MEMORY_MAX_FACTS_PER_USER", 2)
    svc = _service()

    # Border 1: empty current -> new_facts[:N]
    out = _run(svc._consolidate_facts([], ["a", "b", "c"], _FakeLLM()))
    assert out == ["a", "b"]

    # Border 2: empty new -> current_facts[:N]
    out = _run(svc._consolidate_facts(["a", "b", "c"], [], _FakeLLM()))
    assert out == ["a", "b"]

    # Border 3: valid JSON but NOT a list -> new_facts[:N]
    out = _run(
        svc._consolidate_facts(
            ["x", "y", "z"], ["a", "b", "c"], _FakeLLM(content='{"not": "a list"}')
        )
    )
    assert out == ["a", "b"]

    # Border 4: ainvoke raises -> dict.fromkeys(new + current)[:N]
    out = _run(
        svc._consolidate_facts(["c", "d"], ["a", "b"], _RaisingLLM())
    )
    assert out == ["a", "b"]


def test_consolidate_sanitize_path_respects_max_facts_constant(monkeypatch):
    """The valid-list sanitize path also truncates to the constant (not 8)."""
    monkeypatch.setattr(ms, "MEMORY_MAX_FACTS_PER_USER", 2)
    svc = _service()
    llm = _FakeLLM(content='["f1", "f2", "f3", "f4"]')
    out = _run(svc._consolidate_facts(["antigo"], ["novo"], llm))
    assert out == ["f1", "f2"]


# --------------------------------------------------------------------------- #
# g6 / g7 — process_summarization ORCHESTRATION (SPEC §8.2, lines 254-257).
#
# These tests stub the per-step I/O methods on the instance and prove the
# orchestration contract WITHOUT touching Supabase or a real LLM:
#   - delegates the WhatsApp sliding-window step to memory_core (g6 delegate)
#   - saves user memory + session summary on the happy path (g6 persist)
#   - releases the lock in `finally`, even when a step raises (g7)
#   - swallows step errors without propagating (g6 error-swallow)
#   - aborts (no work, no release) when the lock is already held
# --------------------------------------------------------------------------- #
def _make_async(*, record_list=None, return_value=None, raises=None):
    """Build an async stub that optionally records calls / returns / raises."""

    async def _fn(*args, **kwargs):
        if record_list is not None:
            record_list.append({"args": args, "kwargs": kwargs})
        if raises is not None:
            raise raises
        return return_value

    return _fn


def _stub_happy_path(svc, *, released, saved_mem, saved_sum):
    """Wire all per-step methods for a successful run, recording persistence."""
    svc._acquire_lock = _make_async(return_value=True)
    svc._release_lock = _make_async(record_list=released)
    svc._get_existing_facts = _make_async(return_value=["antigo"])
    svc.extract_user_facts = _make_async(return_value=["novo fato"])
    svc.save_user_memory = _make_async(record_list=saved_mem)
    svc.get_user_memory = _make_async(return_value={"facts": []})
    svc.generate_session_summary = _make_async(
        return_value={"summary": "resumo", "topics": [], "decisions": [], "pending_items": []}
    )
    svc.save_session_summary = _make_async(record_list=saved_sum)
    svc._get_memory_llm = lambda *a, **k: object()


_BOTH_ENABLED = {"extract_user_profile": True, "extract_session_summary": True}


def test_process_summarization_saves_memory_and_summary():
    """Happy path: extracted facts saved AND summary saved; lock released once."""
    svc = _service()
    released, saved_mem, saved_sum = [], [], []
    _stub_happy_path(svc, released=released, saved_mem=saved_mem, saved_sum=saved_sum)

    _run(
        svc.process_summarization(
            session_id="s",
            user_id="u",
            company_id="c",
            messages=SAMPLE_MESSAGES,
            channel="web",
            settings=_BOTH_ENABLED,
        )
    )

    assert len(saved_mem) == 1
    assert saved_mem[0]["args"][2] == ["novo fato"]  # positional new_facts
    assert len(saved_sum) == 1
    assert saved_sum[0]["kwargs"]["summary_data"]["summary"] == "resumo"
    assert len(released) == 1  # lock released exactly once


def test_process_summarization_releases_lock_on_error_g7():
    """g7: a raising step is swallowed and the lock is still released in finally."""
    svc = _service()
    released = []
    svc._acquire_lock = _make_async(return_value=True)
    svc._release_lock = _make_async(record_list=released)
    svc._get_existing_facts = _make_async(return_value=[])
    svc.extract_user_facts = _make_async(raises=RuntimeError("boom"))
    svc._get_memory_llm = lambda *a, **k: object()

    # Must NOT raise — asyncio.run would propagate if process_summarization did.
    _run(
        svc.process_summarization(
            session_id="s",
            user_id="u",
            company_id="c",
            messages=SAMPLE_MESSAGES,
            settings={"extract_user_profile": True},
        )
    )

    assert len(released) == 1


class _BaseBoom(BaseException):
    """NOT an Exception subclass -> bypasses `except Exception`, so the lock can
    only be released by the `finally`. This makes the test below kill both the
    'delete the finally' mutation AND the weaker 'dedent release to after the
    except' mutation — the swallowed-error test above survives the latter."""


def test_process_summarization_finally_releases_on_uncaught_exit_g7():
    """g7 (strict): a BaseException skips `except Exception` and propagates; the
    lock is still released, which is ONLY possible via the `finally`."""
    svc = _service()
    released = []
    svc._acquire_lock = _make_async(return_value=True)
    svc._release_lock = _make_async(record_list=released)
    svc._get_existing_facts = _make_async(return_value=[])
    svc.extract_user_facts = _make_async(raises=_BaseBoom())
    svc._get_memory_llm = lambda *a, **k: object()

    propagated = False
    try:
        _run(
            svc.process_summarization(
                session_id="s",
                user_id="u",
                company_id="c",
                messages=SAMPLE_MESSAGES,
                settings={"extract_user_profile": True},
            )
        )
    except _BaseBoom:
        propagated = True

    assert propagated  # not caught by `except Exception` -> bubbles out
    assert len(released) == 1  # only the `finally` could have released the lock


def test_process_summarization_aborts_when_lock_held():
    """Lock already held -> early return: no work done, no release in finally."""
    svc = _service()
    released, extract_calls = [], []
    svc._acquire_lock = _make_async(return_value=False)
    svc._release_lock = _make_async(record_list=released)
    svc.extract_user_facts = _make_async(record_list=extract_calls, return_value=[])
    svc._get_memory_llm = lambda *a, **k: object()

    _run(
        svc.process_summarization(
            session_id="s",
            user_id="u",
            company_id="c",
            messages=SAMPLE_MESSAGES,
            settings=_BOTH_ENABLED,
        )
    )

    assert extract_calls == []  # never entered the try body
    assert released == []  # returned before the try/finally


def test_process_summarization_delegates_sliding_window_to_core(monkeypatch):
    """g6: WhatsApp sliding_window mode delegates the cut to memory_core."""
    svc = _service()
    released, core_calls = [], []

    def _fake_window(messages, window_size):
        core_calls.append((messages, window_size))
        return {"to_summarize": [], "keep_raw": messages}

    monkeypatch.setattr(ms.memory_core, "apply_sliding_window", _fake_window)
    svc._acquire_lock = _make_async(return_value=True)
    svc._release_lock = _make_async(record_list=released)
    svc._get_memory_llm = lambda *a, **k: object()

    _run(
        svc.process_summarization(
            session_id="s",
            user_id="u",
            company_id="c",
            messages=SAMPLE_MESSAGES,
            channel="whatsapp",
            settings={
                "whatsapp_summarization_mode": "sliding_window",
                "whatsapp_sliding_window_size": 1,
            },
        )
    )

    assert len(core_calls) == 1
    assert core_calls[0][1] == 1  # window_size forwarded from settings
    # to_summarize empty -> early `return` inside the try releases the lock,
    # and the `finally` releases it again: 2 releases (verbatim behavior, the
    # lock release is idempotent so this is harmless — pinned as-is).
    assert len(released) == 2
