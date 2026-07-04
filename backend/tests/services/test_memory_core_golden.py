"""
Golden / characterization tests — MemoryService CURRENT behavior (SPEC Passo 1 / M0).

These pin what `app.services.memory_service.MemoryService` does TODAY, so the
later extraction into `memory_core.py` (M1+) can be proven behavior-identical by
snapshot equality. `memory_core.py` does NOT exist yet and is NOT imported here.

Scope (SPEC §8.1, ASYNC branch only — the hot path):
  - Prompt snapshots (exact string equality) for extract_user_facts,
    generate_session_summary, _consolidate_facts. Captured by passing
    a FAKE llm whose `ainvoke` records the prompt and returns canned JSON.
  - Parsers / response handling: valid JSON list / summary dict, ```json fence,
    bare ``` fence, invalid JSON -> [] / None, isinstance(str) filter on facts.
  - Truncation / sanitization in _consolidate_facts (CURRENT hardcoded
    150/8 — pinned, NOT fixed; that is M2b).
  - should_summarize: all web + whatsapp modes + default, characterized on the
    core fn via the injectable `now=` param (FROZEN_NOW, NAIVE). Time-based modes
    mix aware/naive last_message_at to pin the TypeError. See section C4.
  - apply_sliding_window, format_messages_for_prompt — core fns, section C5.
  - build_memory_context formatting (shell), format_memory_context (core).

House convention: asyncio.run for async calls, plain asserts, no pytest-asyncio,
dummy env seeded BEFORE importing app.* (tests/services/conftest.py also seeds,
this is defensive).
"""

from __future__ import annotations

import os

# app.core.config instantiates Settings() eagerly at import time and needs a
# minimal set of env vars. Seed dummies BEFORE importing app.* — mirrors
# tests/test_usage_cost.py and tests/services/conftest.py. No external service
# is touched (Supabase + LLM are fakes).
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
import json  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

from app.services import memory_core  # noqa: E402
from app.services.memory_service import MemoryService  # noqa: E402

# Limits the CURRENT async path hardcodes (150/8) — passed to the core fns so
# they reproduce the live shell's output byte-for-byte. NOT a fix (that is M2b).
CORE_MAX_CHARS = 150
CORE_MAX_FACTS = 8
# Context-formatting limits, matching app.core.constants today.
CTX_MAX_FACTS = 10
CTX_MAX_SUMMARIES = 3
CTX_MAX_PENDING = 5
CTX_PREVIEW_CHARS = 200


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #
class _FakeLLM:
    """Records the prompt passed to ainvoke and returns a canned content."""

    def __init__(self, content: str = "[]", raise_exc: Exception | None = None):
        self._content = content
        self._raise_exc = raise_exc
        self.captured_prompt: str | None = None

    async def ainvoke(self, prompt):
        self.captured_prompt = prompt
        if self._raise_exc is not None:
            raise self._raise_exc
        return SimpleNamespace(content=self._content)


class _Msg:
    """Minimal LangChain-message stand-in: has .type and .content."""

    def __init__(self, type_: str, content: str):
        self.type = type_
        self.content = content


def _service() -> MemoryService:
    """MemoryService with the Supabase client stubbed out (dummy object)."""
    return MemoryService(supabase_client=object())


def _run(coro):
    return asyncio.run(coro)


# Canned LLM contents driving the parser characterization.
VALID_FACTS_JSON = '["fato 1", "fato 2"]'
VALID_SUMMARY_JSON = (
    '{"summary": "resumo", "topics": ["t1"], '
    '"decisions": [], "pending_items": ["p1"]}'
)

SAMPLE_MESSAGES = [
    _Msg("human", "Oi, sou o Breno"),
    _Msg("ai", "Olá Breno!"),
]


# --------------------------------------------------------------------------- #
# 1. PROMPT SNAPSHOTS (exact string equality)
#    Technique: a FakeLLM records the prompt argument; the async method builds
#    the prompt inline and calls llm.ainvoke(prompt). We then assert the captured
#    prompt equals an embedded golden string.
# --------------------------------------------------------------------------- #
def test_extract_facts_prompt_snapshot():
    svc = _service()
    llm = _FakeLLM(content=VALID_FACTS_JSON)
    _run(svc.extract_user_facts(SAMPLE_MESSAGES, existing_facts=["já sei X"], llm=llm))

    expected = """Analise a conversa abaixo e extraia APENAS fatos DURÁVEIS e IMPORTANTES sobre o usuário.

EXTRAIA:
- Informações profissionais (cargo, empresa, departamento, projetos)
- Preferências de comunicação (formal/informal, respostas longas/curtas)
- Interesses e tópicos recorrentes
- Decisões tomadas que afetam o futuro
- Compromissos ou pendências mencionadas

NÃO EXTRAIA:
- Cumprimentos e small talk
- Perguntas genéricas sem contexto pessoal
- Informações já conhecidas/repetidas
- Opiniões momentâneas sem impacto duradouro

FATOS JÁ CONHECIDOS (evite duplicar ou contradizer sem necessidade):
- já sei X

CONVERSA:
Usuário: Oi, sou o Breno
Assistente: Olá Breno!

Responda APENAS com uma lista JSON de novos fatos (strings curtas e objetivas):
["fato 1", "fato 2", "fato 3"]

Se não houver fatos novos relevantes, responda: []"""

    assert llm.captured_prompt == expected


def test_extract_facts_prompt_snapshot_no_existing_facts():
    """The 'FATOS JÁ CONHECIDOS' block renders '(nenhum)' when no facts given."""
    svc = _service()
    llm = _FakeLLM(content=VALID_FACTS_JSON)
    _run(svc.extract_user_facts(SAMPLE_MESSAGES, existing_facts=None, llm=llm))
    assert "FATOS JÁ CONHECIDOS (evite duplicar ou contradizer sem necessidade):\n(nenhum)" in (
        llm.captured_prompt
    )


def test_session_summary_prompt_snapshot_with_context():
    svc = _service()
    llm = _FakeLLM(content=VALID_SUMMARY_JSON)
    _run(
        svc.generate_session_summary(
            SAMPLE_MESSAGES, user_context={"facts": ["é dev", "gosta de Python"]}, llm=llm
        )
    )

    expected = """Gere um resumo estruturado da conversa abaixo.

CONTEXTO DO USUÁRIO:
- é dev
- gosta de Python

CONVERSA:
Usuário: Oi, sou o Breno
Assistente: Olá Breno!

Responda em JSON com a seguinte estrutura:
{
    "summary": "Resumo narrativo de 2-4 frases descrevendo o que foi discutido e concluído",
    "topics": ["tópico1", "tópico2"],
    "decisions": ["decisão tomada pelo usuário"],
    "pending_items": ["pendência ou follow-up necessário"]
}

Se algum campo não se aplicar, use array vazio [].
Responda APENAS o JSON, sem texto adicional."""

    assert llm.captured_prompt == expected


def test_session_summary_prompt_snapshot_no_context():
    """With no user_context the CONTEXTO block is empty (just a blank line)."""
    svc = _service()
    llm = _FakeLLM(content=VALID_SUMMARY_JSON)
    _run(svc.generate_session_summary(SAMPLE_MESSAGES, user_context=None, llm=llm))

    expected = """Gere um resumo estruturado da conversa abaixo.

CONVERSA:
Usuário: Oi, sou o Breno
Assistente: Olá Breno!

Responda em JSON com a seguinte estrutura:
{
    "summary": "Resumo narrativo de 2-4 frases descrevendo o que foi discutido e concluído",
    "topics": ["tópico1", "tópico2"],
    "decisions": ["decisão tomada pelo usuário"],
    "pending_items": ["pendência ou follow-up necessário"]
}

Se algum campo não se aplicar, use array vazio [].
Responda APENAS o JSON, sem texto adicional."""

    assert llm.captured_prompt == expected


def test_consolidate_facts_prompt_snapshot():
    """
    Pins the ASYNC consolidate prompt verbatim, including the literal
    'MÁXIMO 8 fatos no total' and the ABSENCE of the sync-only
    'Make.com -> N8N' few-shot example (OQ-2: async text is canonical).
    """
    svc = _service()
    llm = _FakeLLM(content=VALID_FACTS_JSON)
    current = ["fato antigo A", "fato antigo B"]
    new = ["fato novo C"]
    _run(svc._consolidate_facts(current, new, llm=llm))

    expected = """Você é um Gerente de Memória de uma IA.
Sua função é manter a lista de fatos sobre o usuário ATUALIZADA, CONCISA e SEM DUPLICATAS.

FATOS ANTIGOS (memória existente):
["fato antigo A", "fato antigo B"]

NOVOS FATOS (extraídos da conversa AGORA):
["fato novo C"]

REGRAS DE DISTRIBUIÇÃO (OBRIGATÓRIO):
- MÁXIMO 8 fatos no total
- ATÉ 6 fatos de IDENTIDADE (nome, cargo, empresa, preferências pessoais como hobbies, gostos)
- MÍNIMO 2 fatos de CONTEXTO ATUAL (projetos, ferramentas, tópicos que está trabalhando AGORA)

INSTRUÇÕES:
1. Os NOVOS FATOS representam o contexto ATUAL do usuário.
2. Se um FATO ANTIGO de contexto não tem mais relação com os temas atuais, REMOVA-O.
3. Fatos de IDENTIDADE são permanentes (nome, cargo, hobbies) - só remova se contraditos.
4. PRIORIZE os 2 fatos de contexto mais recentes/relevantes da conversa atual.
5. Se houver contradição, o NOVO fato prevalece.
6. SEJA CONCISO: Cada fato deve ter no máximo 15 palavras.

Retorne APENAS uma lista JSON de strings: ["fato 1", "fato 2"]"""

    assert llm.captured_prompt == expected
    # Explicitly pin the absence of the sync-only example.
    assert "Make.com" not in llm.captured_prompt
    assert "MÁXIMO 8 fatos no total" in llm.captured_prompt


# --------------------------------------------------------------------------- #
# 2. PARSERS / RESPONSE HANDLING (driven through the async methods)
# --------------------------------------------------------------------------- #
def test_extract_facts_parse_valid_list():
    svc = _service()
    llm = _FakeLLM(content='["a", "b"]')
    out = _run(svc.extract_user_facts(SAMPLE_MESSAGES, existing_facts=[], llm=llm))
    assert out == ["a", "b"]


def test_extract_facts_parse_json_fence():
    svc = _service()
    llm = _FakeLLM(content='```json\n["a", "b"]\n```')
    out = _run(svc.extract_user_facts(SAMPLE_MESSAGES, existing_facts=[], llm=llm))
    assert out == ["a", "b"]


def test_extract_facts_parse_bare_fence():
    svc = _service()
    llm = _FakeLLM(content='```\n["a", "b"]\n```')
    out = _run(svc.extract_user_facts(SAMPLE_MESSAGES, existing_facts=[], llm=llm))
    assert out == ["a", "b"]


def test_extract_facts_parse_invalid_returns_empty():
    svc = _service()
    llm = _FakeLLM(content="not json at all")
    out = _run(svc.extract_user_facts(SAMPLE_MESSAGES, existing_facts=[], llm=llm))
    assert out == []


def test_extract_facts_isinstance_str_filter():
    """Non-str and empty/whitespace-only items are filtered out."""
    svc = _service()
    llm = _FakeLLM(content='["keep", 123, "", "   ", null, "also keep"]')
    out = _run(svc.extract_user_facts(SAMPLE_MESSAGES, existing_facts=[], llm=llm))
    assert out == ["keep", "also keep"]


def test_extract_facts_non_list_json_returns_empty():
    """Valid JSON that is not a list (e.g. a dict) -> []."""
    svc = _service()
    llm = _FakeLLM(content='{"not": "a list"}')
    out = _run(svc.extract_user_facts(SAMPLE_MESSAGES, existing_facts=[], llm=llm))
    assert out == []


def test_extract_facts_empty_messages_short_circuit():
    """No messages -> [] without invoking the LLM."""
    svc = _service()
    llm = _FakeLLM(content='["x"]')
    out = _run(svc.extract_user_facts([], existing_facts=[], llm=llm))
    assert out == []
    assert llm.captured_prompt is None


def test_session_summary_parse_valid_dict():
    svc = _service()
    llm = _FakeLLM(content=VALID_SUMMARY_JSON)
    out = _run(svc.generate_session_summary(SAMPLE_MESSAGES, user_context=None, llm=llm))
    assert out == {
        "summary": "resumo",
        "topics": ["t1"],
        "decisions": [],
        "pending_items": ["p1"],
    }


def test_session_summary_parse_json_fence():
    svc = _service()
    llm = _FakeLLM(content="```json\n" + VALID_SUMMARY_JSON + "\n```")
    out = _run(svc.generate_session_summary(SAMPLE_MESSAGES, user_context=None, llm=llm))
    assert out["summary"] == "resumo"


def test_session_summary_parse_bare_fence():
    svc = _service()
    llm = _FakeLLM(content="```\n" + VALID_SUMMARY_JSON + "\n```")
    out = _run(svc.generate_session_summary(SAMPLE_MESSAGES, user_context=None, llm=llm))
    assert out["summary"] == "resumo"


def test_session_summary_parse_invalid_returns_none():
    svc = _service()
    llm = _FakeLLM(content="totally broken")
    out = _run(svc.generate_session_summary(SAMPLE_MESSAGES, user_context=None, llm=llm))
    assert out is None


def test_session_summary_empty_messages_short_circuit():
    svc = _service()
    llm = _FakeLLM(content=VALID_SUMMARY_JSON)
    out = _run(svc.generate_session_summary([], user_context=None, llm=llm))
    assert out is None
    assert llm.captured_prompt is None


def test_consolidate_parse_valid_list():
    svc = _service()
    llm = _FakeLLM(content='["x", "y"]')
    out = _run(svc._consolidate_facts(["old"], ["new"], llm=llm))
    assert out == ["x", "y"]


def test_consolidate_parse_json_fence():
    svc = _service()
    llm = _FakeLLM(content='```json\n["x", "y"]\n```')
    out = _run(svc._consolidate_facts(["old"], ["new"], llm=llm))
    assert out == ["x", "y"]


def test_consolidate_parse_bare_fence():
    svc = _service()
    llm = _FakeLLM(content='```\n["x", "y"]\n```')
    out = _run(svc._consolidate_facts(["old"], ["new"], llm=llm))
    assert out == ["x", "y"]


def test_consolidate_parse_invalid_uses_exception_fallback():
    """
    Invalid JSON raises inside json.loads -> except branch -> merge fallback
    dedup(new + current)[:8].
    """
    svc = _service()
    llm = _FakeLLM(content="garbage")
    out = _run(svc._consolidate_facts(["old1", "old2"], ["new1"], llm=llm))
    assert out == ["new1", "old1", "old2"]


def test_consolidate_non_list_json_returns_new_facts_slice():
    """Valid JSON but not a list -> returns new_facts[:8] (current border behavior)."""
    svc = _service()
    llm = _FakeLLM(content='{"not": "list"}')
    new = [f"n{i}" for i in range(10)]
    out = _run(svc._consolidate_facts(["old"], new, llm=llm))
    assert out == new[:8]


# --------------------------------------------------------------------------- #
# 3. TRUNCATION / SANITIZATION in _consolidate_facts
#    Pins CURRENT hardcoded 150/8 (do NOT fix — that is M2b).
# --------------------------------------------------------------------------- #
def test_consolidate_truncates_long_fact_to_147_plus_ellipsis():
    """A fact > 150 chars is cut to 147 chars + '...' (total 150)."""
    svc = _service()
    long_fact = "a" * 200
    llm = _FakeLLM(content=json.dumps([long_fact]))
    out = _run(svc._consolidate_facts(["old"], ["new"], llm=llm))
    assert len(out) == 1
    assert out[0] == "a" * 147 + "..."
    assert len(out[0]) == 150


def test_consolidate_fact_at_boundary_not_truncated():
    """A fact of exactly 150 chars is NOT truncated (boundary: > 150 only)."""
    svc = _service()
    fact_150 = "b" * 150
    llm = _FakeLLM(content=json.dumps([fact_150]))
    out = _run(svc._consolidate_facts(["old"], ["new"], llm=llm))
    assert out == [fact_150]


def test_consolidate_fact_151_chars_truncated():
    """A fact of 151 chars IS truncated (just over the boundary)."""
    svc = _service()
    fact_151 = "c" * 151
    llm = _FakeLLM(content=json.dumps([fact_151]))
    out = _run(svc._consolidate_facts(["old"], ["new"], llm=llm))
    assert out == ["c" * 147 + "..."]


def test_consolidate_limits_to_8_facts():
    """More than 8 returned facts are truncated to the first 8."""
    svc = _service()
    many = [f"fact{i}" for i in range(12)]
    llm = _FakeLLM(content=json.dumps(many))
    out = _run(svc._consolidate_facts(["old"], ["new"], llm=llm))
    assert out == many[:8]
    assert len(out) == 8


def test_consolidate_drops_empty_facts():
    """Whitespace-only / empty facts are discarded during sanitization."""
    svc = _service()
    llm = _FakeLLM(content=json.dumps(["keep", "", "   ", "also"]))
    out = _run(svc._consolidate_facts(["old"], ["new"], llm=llm))
    assert out == ["keep", "also"]


def test_consolidate_empty_current_returns_new_slice():
    """not current_facts early-return -> new_facts[:8] (border :1567)."""
    svc = _service()
    llm = _FakeLLM(content='["should not be used"]')
    new = [f"n{i}" for i in range(10)]
    out = _run(svc._consolidate_facts([], new, llm=llm))
    assert out == new[:8]
    assert llm.captured_prompt is None  # short-circuits before ainvoke


def test_consolidate_empty_new_returns_current_slice():
    """not new_facts early-return -> current_facts[:8] (border :1570)."""
    svc = _service()
    llm = _FakeLLM(content='["should not be used"]')
    current = [f"c{i}" for i in range(10)]
    out = _run(svc._consolidate_facts(current, [], llm=llm))
    assert out == current[:8]
    assert llm.captured_prompt is None


def test_consolidate_slices_to_8_before_dropping_empties():
    """
    SLICE-BEFORE-SANITIZE: consolidated[:8] is taken FIRST, THEN empties dropped.
    Input has 10 items; the first 8 are ['a','','b','','c','d','e','f'] and the two
    empty strings within that window are dropped -> 6 items. Items at indices 8,9
    ('g','h') never reach sanitization. This proves the slice happens before the
    empty-drop (a future M2b fix that drops-then-slices would yield a different set).
    """
    svc = _service()
    items = ["a", "", "b", "", "c", "d", "e", "f", "g", "h"]
    llm = _FakeLLM(content=json.dumps(items))
    out = _run(svc._consolidate_facts(["old"], ["new"], llm=llm))
    assert out == ["a", "b", "c", "d", "e", "f"]
    assert len(out) == 6


def test_consolidate_coerces_non_strings_via_str():
    """
    The consolidate path does NOT filter non-strings (unlike the extract path's
    isinstance(str) filter). Each fact is coerced with str(fact): 123 -> '123',
    True -> 'True'. Pinned as CURRENT behavior.
    """
    svc = _service()
    llm = _FakeLLM(content="[123, \"x\", true]")
    out = _run(svc._consolidate_facts(["old"], ["new"], llm=llm))
    assert out == ["123", "x", "True"]


def test_consolidate_exception_fallback_dedup_with_overlap():
    """
    Non-JSON content makes json.loads raise -> except branch -> fallback merge
    list(dict.fromkeys(new_facts + current_facts))[:8]. With new=['a','b'] and
    current=['b','c'] the concat ['a','b','b','c'] dedups (order-preserving) to
    ['a','b','c']. Pinned as CURRENT behavior.
    """
    svc = _service()
    llm = _FakeLLM(content="not json garbage {")
    out = _run(svc._consolidate_facts(["b", "c"], ["a", "b"], llm=llm))
    assert out == ["a", "b", "c"]


# --------------------------------------------------------------------------- #
# Frozen NAIVE clock shared by the core should_summarize tests (section C4).
# --------------------------------------------------------------------------- #
FROZEN_NOW = datetime(2026, 5, 29, 12, 0, 0)


# --------------------------------------------------------------------------- #
# 7. build_memory_context formatting
#    Faked I/O: get_user_memory / get_recent_summaries are replaced
#    on the instance to supply canned facts / summaries.
# --------------------------------------------------------------------------- #
def _ctx_service(facts, summaries):
    svc = _service()

    async def _fake_user_memory(user_id, company_id, agent_id=None):
        return {"facts": facts} if facts is not None else {}

    async def _fake_summaries(user_id, company_id, limit=5, agent_id=None):
        return summaries

    svc.get_user_memory = _fake_user_memory
    svc.get_recent_summaries = _fake_summaries
    return svc


def test_build_context_full_blocks():
    summaries = [
        {
            "created_at": "2026-05-20T10:00:00Z",
            "summary": "Discutimos o projeto X",
            "pending_items": ["enviar proposta"],
        }
    ]
    svc = _ctx_service(facts=["é dev", "usa Python"], summaries=summaries)
    out = _run(svc.build_memory_context("u1", "c1"))

    expected = (
        "**Sobre este usuário:**\n"
        "- é dev\n"
        "- usa Python\n"
        "\n**Conversas anteriores relevantes:**\n"
        "- 20/05: Discutimos o projeto X\n"
        "\n**Pendências identificadas:**\n"
        "- enviar proposta"
    )
    assert out == expected


def test_build_context_empty_returns_empty_string():
    svc = _ctx_service(facts=None, summaries=[])
    out = _run(svc.build_memory_context("u1", "c1"))
    assert out == ""


def test_build_context_facts_only():
    svc = _ctx_service(facts=["fato único"], summaries=[])
    out = _run(svc.build_memory_context("u1", "c1"))
    assert out == "**Sobre este usuário:**\n- fato único"


def test_build_context_facts_respect_max_facts_tail():
    """When facts exceed max_facts, the TAIL (most recent) is kept."""
    facts = [f"f{i}" for i in range(15)]
    svc = _ctx_service(facts=facts, summaries=[])
    out = _run(svc.build_memory_context("u1", "c1", max_facts=3))
    assert out == "**Sobre este usuário:**\n- f12\n- f13\n- f14"


def test_build_context_summary_preview_truncation():
    """Summary text is sliced to MEMORY_SUMMARY_PREVIEW_MAX_CHARS (200)."""
    long_summary = "z" * 300
    summaries = [{"created_at": "2026-01-02T00:00:00Z", "summary": long_summary, "pending_items": []}]
    svc = _ctx_service(facts=None, summaries=summaries)
    out = _run(svc.build_memory_context("u1", "c1"))
    assert "- 02/01: " + "z" * 200 in out
    assert "z" * 201 not in out


def test_build_context_pending_items_limited():
    """Pending items across summaries are capped at MEMORY_CONTEXT_MAX_PENDING_ITEMS (5)."""
    summaries = [
        {
            "created_at": "2026-03-03T00:00:00Z",
            "summary": "s",
            "pending_items": [f"p{i}" for i in range(8)],
        }
    ]
    svc = _ctx_service(facts=None, summaries=summaries)
    out = _run(svc.build_memory_context("u1", "c1"))
    pending_block = out.split("**Pendências identificadas:**\n")[1]
    pending_lines = [ln for ln in pending_block.split("\n") if ln.startswith("- ")]
    assert len(pending_lines) == 5
    assert pending_lines == [f"- p{i}" for i in range(5)]


def test_build_context_bad_created_at_uses_question_mark():
    """A summary with unparseable created_at renders date as '?'."""
    summaries = [{"created_at": "not-a-date", "summary": "texto", "pending_items": []}]
    svc = _ctx_service(facts=None, summaries=summaries)
    out = _run(svc.build_memory_context("u1", "c1"))
    assert "- ?: texto" in out


# =========================================================================== #
# CORE-EQUIVALENCE TESTS (M1) — prove app.services.memory_core reproduces the
# CURRENT async path of MemoryService byte-for-byte. Each test ties the PURE
# core function to the live shell (recorded prompt / shared snapshot / shared
# inputs) so any drift is caught.
# =========================================================================== #


# --------------------------------------------------------------------------- #
# C1. PROMPT BUILDERS — assert core builder == prompt the live shell sent.
# --------------------------------------------------------------------------- #
def test_core_extract_facts_prompt_matches_shell():
    svc = _service()
    llm = _FakeLLM(content=VALID_FACTS_JSON)
    existing = ["já sei X"]
    _run(svc.extract_user_facts(SAMPLE_MESSAGES, existing_facts=existing, llm=llm))

    conversation_text = memory_core.format_messages_for_prompt(SAMPLE_MESSAGES)
    core_prompt = memory_core.build_extract_facts_prompt(conversation_text, existing)
    assert core_prompt == llm.captured_prompt


def test_core_extract_facts_prompt_no_existing_facts():
    """`existing_facts=[]` renders the '(nenhum)' block (shell uses [] when None)."""
    svc = _service()
    llm = _FakeLLM(content=VALID_FACTS_JSON)
    _run(svc.extract_user_facts(SAMPLE_MESSAGES, existing_facts=None, llm=llm))

    conversation_text = memory_core.format_messages_for_prompt(SAMPLE_MESSAGES)
    # Shell normalizes existing_facts=None -> [] before building the prompt.
    core_prompt = memory_core.build_extract_facts_prompt(conversation_text, [])
    assert core_prompt == llm.captured_prompt
    assert "(nenhum)" in core_prompt


def test_core_session_summary_prompt_with_context_matches_shell():
    svc = _service()
    llm = _FakeLLM(content=VALID_SUMMARY_JSON)
    ctx = {"facts": ["é dev", "gosta de Python"]}
    _run(svc.generate_session_summary(SAMPLE_MESSAGES, user_context=ctx, llm=llm))

    conversation_text = memory_core.format_messages_for_prompt(SAMPLE_MESSAGES)
    core_prompt = memory_core.build_session_summary_prompt(conversation_text, ctx)
    assert core_prompt == llm.captured_prompt


def test_core_session_summary_prompt_no_context_matches_shell():
    svc = _service()
    llm = _FakeLLM(content=VALID_SUMMARY_JSON)
    _run(svc.generate_session_summary(SAMPLE_MESSAGES, user_context=None, llm=llm))

    conversation_text = memory_core.format_messages_for_prompt(SAMPLE_MESSAGES)
    core_prompt = memory_core.build_session_summary_prompt(conversation_text, None)
    assert core_prompt == llm.captured_prompt


def test_core_consolidate_prompt_matches_shell():
    svc = _service()
    llm = _FakeLLM(content=VALID_FACTS_JSON)
    current = ["fato antigo A", "fato antigo B"]
    new = ["fato novo C"]
    _run(svc._consolidate_facts(current, new, llm=llm))

    core_prompt = memory_core.build_consolidate_facts_prompt(current, new)
    assert core_prompt == llm.captured_prompt
    assert "MÁXIMO 8 fatos no total" in core_prompt
    assert "Make.com" not in core_prompt


# --------------------------------------------------------------------------- #
# C2. PARSERS — same fence/invalid/None cases as the shell parser goldens.
# --------------------------------------------------------------------------- #
def test_core_parse_extract_facts_valid_list():
    assert memory_core.parse_extract_facts_response('["a", "b"]') == ["a", "b"]


def test_core_parse_extract_facts_json_fence():
    assert memory_core.parse_extract_facts_response('```json\n["a", "b"]\n```') == [
        "a",
        "b",
    ]


def test_core_parse_extract_facts_bare_fence():
    assert memory_core.parse_extract_facts_response('```\n["a", "b"]\n```') == [
        "a",
        "b",
    ]


def test_core_parse_extract_facts_invalid_returns_empty():
    assert memory_core.parse_extract_facts_response("not json at all") == []


def test_core_parse_extract_facts_isinstance_str_filter():
    raw = '["keep", 123, "", "   ", null, "also keep"]'
    assert memory_core.parse_extract_facts_response(raw) == ["keep", "also keep"]


def test_core_parse_extract_facts_non_list_returns_empty():
    assert memory_core.parse_extract_facts_response('{"not": "a list"}') == []


def test_core_parse_session_summary_valid_dict():
    out = memory_core.parse_session_summary_response(VALID_SUMMARY_JSON)
    assert out == {
        "summary": "resumo",
        "topics": ["t1"],
        "decisions": [],
        "pending_items": ["p1"],
    }


def test_core_parse_session_summary_json_fence():
    raw = "```json\n" + VALID_SUMMARY_JSON + "\n```"
    assert memory_core.parse_session_summary_response(raw)["summary"] == "resumo"


def test_core_parse_session_summary_bare_fence():
    raw = "```\n" + VALID_SUMMARY_JSON + "\n```"
    assert memory_core.parse_session_summary_response(raw)["summary"] == "resumo"


def test_core_parse_session_summary_invalid_returns_none():
    assert memory_core.parse_session_summary_response("totally broken") is None


def test_core_parse_consolidate_valid_list():
    out = memory_core.parse_consolidate_facts_response(
        '["x", "y"]', CORE_MAX_CHARS, CORE_MAX_FACTS
    )
    assert out == ["x", "y"]


def test_core_parse_consolidate_json_fence():
    out = memory_core.parse_consolidate_facts_response(
        '```json\n["x", "y"]\n```', CORE_MAX_CHARS, CORE_MAX_FACTS
    )
    assert out == ["x", "y"]


def test_core_parse_consolidate_bare_fence():
    out = memory_core.parse_consolidate_facts_response(
        '```\n["x", "y"]\n```', CORE_MAX_CHARS, CORE_MAX_FACTS
    )
    assert out == ["x", "y"]


def test_core_parse_consolidate_invalid_returns_empty():
    """Core returns [] on invalid JSON (shell decides the merge fallback)."""
    out = memory_core.parse_consolidate_facts_response(
        "garbage", CORE_MAX_CHARS, CORE_MAX_FACTS
    )
    assert out == []


def test_core_parse_consolidate_non_list_returns_empty():
    """Core returns [] on non-list JSON (shell decides new_facts[:8] fallback)."""
    out = memory_core.parse_consolidate_facts_response(
        '{"not": "list"}', CORE_MAX_CHARS, CORE_MAX_FACTS
    )
    assert out == []


# --------------------------------------------------------------------------- #
# C3. sanitize_facts(..., 150, 8) — reproduce the consolidate truncation goldens.
# --------------------------------------------------------------------------- #
def test_core_sanitize_truncates_long_fact():
    out = memory_core.sanitize_facts(["a" * 200], CORE_MAX_CHARS, CORE_MAX_FACTS)
    assert out == ["a" * 147 + "..."]
    assert len(out[0]) == 150


def test_core_sanitize_boundary_150_not_truncated():
    fact_150 = "b" * 150
    assert memory_core.sanitize_facts([fact_150], CORE_MAX_CHARS, CORE_MAX_FACTS) == [
        fact_150
    ]


def test_core_sanitize_151_truncated():
    assert memory_core.sanitize_facts(["c" * 151], CORE_MAX_CHARS, CORE_MAX_FACTS) == [
        "c" * 147 + "..."
    ]


def test_core_sanitize_limits_to_8():
    many = [f"fact{i}" for i in range(12)]
    out = memory_core.sanitize_facts(many, CORE_MAX_CHARS, CORE_MAX_FACTS)
    assert out == many[:8]
    assert len(out) == 8


def test_core_sanitize_drops_empties():
    out = memory_core.sanitize_facts(
        ["keep", "", "   ", "also"], CORE_MAX_CHARS, CORE_MAX_FACTS
    )
    assert out == ["keep", "also"]


def test_core_sanitize_slices_before_dropping_empties():
    """SLICE-BEFORE-SANITIZE: first 8 taken, THEN empties dropped (matches shell)."""
    items = ["a", "", "b", "", "c", "d", "e", "f", "g", "h"]
    out = memory_core.sanitize_facts(items, CORE_MAX_CHARS, CORE_MAX_FACTS)
    assert out == ["a", "b", "c", "d", "e", "f"]
    assert len(out) == 6


def test_core_sanitize_coerces_non_strings():
    out = memory_core.sanitize_facts([123, "x", True], CORE_MAX_CHARS, CORE_MAX_FACTS)
    assert out == ["123", "x", "True"]


def test_core_parse_consolidate_full_sanitization_matches_shell():
    """End-to-end: core parser+sanitizer == shell sanitized output for same JSON."""
    items = ["a" * 200, "", "  ", "ok", 7]
    raw = json.dumps(items)
    svc = _service()
    llm = _FakeLLM(content=raw)
    shell_out = _run(svc._consolidate_facts(["old"], ["new"], llm=llm))
    core_out = memory_core.parse_consolidate_facts_response(
        raw, CORE_MAX_CHARS, CORE_MAX_FACTS
    )
    assert core_out == shell_out


# --------------------------------------------------------------------------- #
# C4. should_summarize core fn — frozen NAIVE `now=` reproduces every golden.
# --------------------------------------------------------------------------- #
def test_core_should_summarize_web_session_end():
    assert (
        memory_core.should_summarize(
            {"web_summarization_mode": "session_end"},
            "web",
            3,
            FROZEN_NOW,
            session_ended=True,
        )
        is True
    )


def test_core_should_summarize_web_session_end_not_ended():
    assert (
        memory_core.should_summarize(
            {"web_summarization_mode": "session_end"},
            "web",
            3,
            FROZEN_NOW,
            session_ended=False,
        )
        is False
    )


def test_core_should_summarize_web_message_count():
    s = {"web_summarization_mode": "message_count", "web_message_threshold": 10}
    assert memory_core.should_summarize(s, "web", 10, FROZEN_NOW) is True
    assert memory_core.should_summarize(s, "web", 20, FROZEN_NOW) is True
    assert memory_core.should_summarize(s, "web", 15, FROZEN_NOW) is False
    assert memory_core.should_summarize(s, "web", 5, FROZEN_NOW) is False


def test_core_should_summarize_web_inactivity():
    s = {"web_summarization_mode": "inactivity", "web_inactivity_timeout_min": 30}
    last_old = FROZEN_NOW - timedelta(minutes=45)
    last_new = FROZEN_NOW - timedelta(minutes=10)
    assert memory_core.should_summarize(s, "web", 3, last_old, now=FROZEN_NOW) is True
    assert memory_core.should_summarize(s, "web", 3, last_new, now=FROZEN_NOW) is False


def test_core_should_summarize_naive_arithmetic_explicit():
    s = {"web_summarization_mode": "inactivity", "web_inactivity_timeout_min": 30}
    last = datetime(2026, 5, 29, 9, 0, 0)  # naive, 3h before FROZEN_NOW
    assert memory_core.should_summarize(s, "web", 3, last, now=FROZEN_NOW) is True


def test_core_should_summarize_aware_last_message_raises_typeerror():
    """NAIVE now - AWARE last_message_at raises TypeError (pinned)."""
    aware = datetime(2026, 5, 29, tzinfo=timezone.utc)
    s = {"web_summarization_mode": "inactivity", "web_inactivity_timeout_min": 30}
    with pytest.raises(TypeError):
        memory_core.should_summarize(s, "web", 3, aware, now=FROZEN_NOW)


def test_core_should_summarize_whatsapp_message_count():
    s = {"whatsapp_summarization_mode": "message_count", "whatsapp_message_threshold": 50}
    assert memory_core.should_summarize(s, "whatsapp", 50, FROZEN_NOW) is True
    assert memory_core.should_summarize(s, "whatsapp", 49, FROZEN_NOW) is False


def test_core_should_summarize_whatsapp_sliding_window():
    s = {
        "whatsapp_summarization_mode": "sliding_window",
        "whatsapp_sliding_window_size": 50,
        "whatsapp_message_threshold": 60,
    }
    assert memory_core.should_summarize(s, "whatsapp", 60, FROZEN_NOW) is True
    assert memory_core.should_summarize(s, "whatsapp", 55, FROZEN_NOW) is False


def test_core_should_summarize_whatsapp_time_based():
    s = {"whatsapp_summarization_mode": "time_based", "whatsapp_time_interval_hours": 24}
    last_old = FROZEN_NOW - timedelta(hours=25)
    last_new = FROZEN_NOW - timedelta(hours=2)
    assert memory_core.should_summarize(s, "whatsapp", 3, last_old, now=FROZEN_NOW) is True
    assert memory_core.should_summarize(s, "whatsapp", 3, last_new, now=FROZEN_NOW) is False


def test_core_should_summarize_default_false():
    assert memory_core.should_summarize({}, "unknown_channel", 100, FROZEN_NOW) is False


# --------------------------------------------------------------------------- #
# C5. apply_sliding_window / format_messages_for_prompt — reproduce goldens.
# --------------------------------------------------------------------------- #
def test_core_sliding_window_under_or_equal():
    msgs = [1, 2, 3]
    assert memory_core.apply_sliding_window(msgs, 5) == {
        "to_summarize": [],
        "keep_raw": msgs,
    }
    assert memory_core.apply_sliding_window(msgs, 3) == {
        "to_summarize": [],
        "keep_raw": msgs,
    }


def test_core_sliding_window_over_cut():
    msgs = list(range(70))
    out = memory_core.apply_sliding_window(msgs, 50)
    assert out["to_summarize"] == msgs[:20]
    assert out["keep_raw"] == msgs[20:]
    assert len(out["keep_raw"]) == 50


def test_core_format_messages_roles_and_system_ignored():
    msgs = [
        _Msg("human", "pergunta"),
        _Msg("ai", "resposta"),
        _Msg("system", "instrução de sistema"),
    ]
    assert (
        memory_core.format_messages_for_prompt(msgs)
        == "Usuário: pergunta\nAssistente: resposta"
    )


def test_core_format_messages_role_attribute_fallback():
    user_msg = SimpleNamespace(role="user", content="oi")
    assistant_msg = SimpleNamespace(role="assistant", content="olá")
    assert (
        memory_core.format_messages_for_prompt([user_msg, assistant_msg])
        == "Usuário: oi\nAssistente: olá"
    )


def test_core_format_messages_unknown_role_dropped():
    assert memory_core.format_messages_for_prompt([SimpleNamespace(content="x")]) == ""


# --------------------------------------------------------------------------- #
# C6. format_memory_context — reproduce build_memory_context goldens.
# --------------------------------------------------------------------------- #
def _core_ctx(facts, summaries):
    return memory_core.format_memory_context(
        user_facts=facts or [],
        summaries=summaries,
        max_facts=CTX_MAX_FACTS,
        max_summaries=CTX_MAX_SUMMARIES,
        max_pending=CTX_MAX_PENDING,
        preview_chars=CTX_PREVIEW_CHARS,
    )


def test_core_context_full_blocks_matches_shell():
    summaries = [
        {
            "created_at": "2026-05-20T10:00:00Z",
            "summary": "Discutimos o projeto X",
            "pending_items": ["enviar proposta"],
        }
    ]
    facts = ["é dev", "usa Python"]
    svc = _ctx_service(facts=facts, summaries=summaries)
    shell_out = _run(svc.build_memory_context("u1", "c1"))
    expected = (
        "**Sobre este usuário:**\n"
        "- é dev\n"
        "- usa Python\n"
        "\n**Conversas anteriores relevantes:**\n"
        "- 20/05: Discutimos o projeto X\n"
        "\n**Pendências identificadas:**\n"
        "- enviar proposta"
    )
    assert _core_ctx(facts, summaries) == expected
    assert _core_ctx(facts, summaries) == shell_out


def test_core_context_empty_returns_empty_string():
    assert _core_ctx(None, []) == ""


def test_core_context_facts_only():
    assert _core_ctx(["fato único"], []) == "**Sobre este usuário:**\n- fato único"


def test_core_context_facts_respect_max_facts_tail():
    facts = [f"f{i}" for i in range(15)]
    out = memory_core.format_memory_context(
        user_facts=facts,
        summaries=[],
        max_facts=3,
        max_summaries=CTX_MAX_SUMMARIES,
        max_pending=CTX_MAX_PENDING,
        preview_chars=CTX_PREVIEW_CHARS,
    )
    assert out == "**Sobre este usuário:**\n- f12\n- f13\n- f14"


def test_core_context_summary_preview_truncation():
    long_summary = "z" * 300
    summaries = [
        {"created_at": "2026-01-02T00:00:00Z", "summary": long_summary, "pending_items": []}
    ]
    out = _core_ctx(None, summaries)
    assert "- 02/01: " + "z" * 200 in out
    assert "z" * 201 not in out


def test_core_context_pending_items_limited():
    summaries = [
        {
            "created_at": "2026-03-03T00:00:00Z",
            "summary": "s",
            "pending_items": [f"p{i}" for i in range(8)],
        }
    ]
    out = _core_ctx(None, summaries)
    pending_block = out.split("**Pendências identificadas:**\n")[1]
    pending_lines = [ln for ln in pending_block.split("\n") if ln.startswith("- ")]
    assert pending_lines == [f"- p{i}" for i in range(5)]


def test_core_context_bad_created_at_uses_question_mark():
    summaries = [{"created_at": "not-a-date", "summary": "texto", "pending_items": []}]
    assert "- ?: texto" in _core_ctx(None, summaries)
