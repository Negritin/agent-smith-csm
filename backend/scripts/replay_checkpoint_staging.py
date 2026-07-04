#!/usr/bin/env python3
"""
Gate de release (feat-044): Replay de checkpoint do runtime ANTIGO em STAGING.

A SPEC exige, ANTES do merge, provar que um checkpoint LangGraph gravado pelo
runtime ANTIGO — contendo `ToolMessage` de RAG cujo `content` é a string
XML-wrapped (`<rag_context>...</rag_context>`, com escape HTML) — pode ser
recarregado e re-executado pelo `agent_node` NOVO sem reprocessar a busca e sem
quebrar a compatibilidade de `ToolMessage(content=str)`.

Este script materializa esse checkpoint, o grava e relê pelo MESMO checkpointer
de produção e re-executa o `agent_node`. Serve como artefato reproduzível do
gate de staging: imprime um relatório estruturado e sai com código != 0 em
QUALQUER falha (bloqueia o merge).

Uso:
    # Staging real (Postgres):
    STAGING_DB_URL="postgresql://..." python scripts/replay_checkpoint_staging.py

    # Fallback local (MemorySaver — compartilha o JsonPlusSerializer com o
    # AsyncPostgresSaver de produção; usado quando não há DB de staging):
    python scripts/replay_checkpoint_staging.py

Variáveis:
    STAGING_DB_URL / DB_URL : connection string do Postgres de staging. Quando
                              presente, o checkpoint transita pelo
                              AsyncPostgresSaver REAL (fidelidade máxima).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

# Env mínima ANTES de importar app.* (Settings é instanciado em import time).
for _key, _value in {
    "SUPABASE_URL": "https://test.supabase.co",
    "SUPABASE_KEY": "test-key",
    "OPENAI_API_KEY": "sk-test",
    "MINIO_ROOT_USER": "minio",
    "MINIO_ROOT_PASSWORD": "minio123",
    "INTERNAL_JWT_SECRET": "0" * 64,
    "ENCRYPTION_KEY": "SlEoEOtyl89plqTxYcRD-B_9hIBad3-n1rr9isP442Y=",
}.items():
    os.environ.setdefault(_key, _value)

from langchain_core.messages import (  # noqa: E402
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.agents.nodes import agent_node, wrap_prompt_xml  # noqa: E402

RAG_CALL_ID = "call-rag-old-1"


# --------------------------------------------------------------------------- #
# Relatório estruturado (cada check é um gate).
# --------------------------------------------------------------------------- #
class Report:
    def __init__(self) -> None:
        self.checks: List[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, bool(ok), detail))
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.checks)


# --------------------------------------------------------------------------- #
# Sentinela do runtime ANTIGO.
# --------------------------------------------------------------------------- #
def _legacy_rag_inner_json() -> str:
    return json.dumps(
        {
            "found": True,
            "strategy": "hybrid",
            "content": "Flux Pay processa saques em até 2 dias úteis (D+2) & sem taxa <padrão>.",
            "chunks": [{"text": "SLA de saque: D+2.", "score": 0.93}],
            "search_time_ms": 27,
            "agent_id": "agent-replay",
        },
        ensure_ascii=False,
    )


def _legacy_wrapped_rag_content() -> str:
    return wrap_prompt_xml("rag_context", _legacy_rag_inner_json())


def _old_runtime_messages() -> List[Any]:
    return [
        HumanMessage(content="Em quanto tempo cai meu saque?"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "knowledge_base_search",
                    "args": {"query": "prazo de saque"},
                    "id": RAG_CALL_ID,
                }
            ],
        ),
        ToolMessage(
            content=_legacy_wrapped_rag_content(),
            tool_call_id=RAG_CALL_ID,
            name="knowledge_base_search",
        ),
    ]


class _RecordingLLM:
    def __init__(self) -> None:
        self.seen_messages: Optional[List[Any]] = None
        self.invocations = 0

    async def ainvoke(self, messages: List[Any], config: Any = None) -> AIMessage:
        self.invocations += 1
        self.seen_messages = list(messages)
        return AIMessage(
            content="Seu saque cai em até 2 dias úteis (D+2).",
            usage_metadata={"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
        )


# --------------------------------------------------------------------------- #
# Persistência: AsyncPostgresSaver (staging real) ou MemorySaver (fallback).
# Ambos usam o JsonPlusSerializer — mesma serialização de checkpoint.
# --------------------------------------------------------------------------- #
async def _roundtrip_postgres(db_url: str, messages: List[Any]) -> List[Any]:
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(db_url) as saver:
        await saver.setup()
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"messages": messages}
        checkpoint["channel_versions"] = {"messages": 1}
        config = {
            "configurable": {
                "thread_id": "feat-044-replay-staging",
                "checkpoint_ns": "",
            }
        }
        new_config = await saver.aput(
            config, checkpoint, {"source": "loop", "step": 1}, {"messages": 1}
        )
        tup = await saver.aget_tuple(new_config)
        assert tup is not None
        return tup.checkpoint["channel_values"]["messages"]


async def _roundtrip_memory(messages: List[Any]) -> List[Any]:
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.memory import MemorySaver

    saver = MemorySaver()
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": messages}
    checkpoint["channel_versions"] = {"messages": 1}
    config = {"configurable": {"thread_id": "feat-044-replay-local", "checkpoint_ns": ""}}
    new_config = await saver.aput(
        config, checkpoint, {"source": "loop", "step": 1}, {"messages": 1}
    )
    tup = await saver.aget_tuple(new_config)
    assert tup is not None
    return tup.checkpoint["channel_values"]["messages"]


# --------------------------------------------------------------------------- #
# Gate principal.
# --------------------------------------------------------------------------- #
async def run() -> int:
    report = Report()
    db_url = os.environ.get("STAGING_DB_URL") or os.environ.get("DB_URL")

    print("=" * 78)
    print("feat-044 — Replay de checkpoint do runtime ANTIGO (gate de staging)")
    print(f"Timestamp (UTC): {datetime.now(timezone.utc).isoformat()}")
    print(f"Python: {sys.version.split()[0]}")

    backend = "MemorySaver (fallback local — mesmo JsonPlusSerializer de prod)"
    roundtrip = _roundtrip_memory
    if db_url:
        backend = "AsyncPostgresSaver (STAGING real)"
    print(f"Checkpointer: {backend}")
    print("-" * 78)

    original = _old_runtime_messages()
    wrapped = _legacy_wrapped_rag_content()

    # Pré-condições do checkpoint ANTIGO.
    print("[1] Materializando checkpoint do runtime ANTIGO (RAG XML-wrapped)…")
    report.check(
        "Pré-condição: ToolMessage de RAG é XML-wrapped com escape HTML",
        wrapped.startswith("<rag_context>")
        and wrapped.endswith("</rag_context>")
        and "&amp;" in wrapped
        and "&lt;" in wrapped,
        f"len={len(wrapped)}",
    )

    # c1: carregar checkpoint pelo checkpointer real.
    print(f"[2] Gravando e relendo via {backend}…")
    try:
        if db_url:
            restored = await _roundtrip_postgres(db_url, original)
        else:
            restored = await roundtrip(original)
    except Exception as exc:  # pragma: no cover - falha de infra de staging
        print(f"  [FAIL] Round-trip do checkpoint levantou {type(exc).__name__}: {exc}")
        traceback.print_exc()
        report.check("c1: carregar checkpoint do runtime ANTIGO", False, str(exc))
        return _finish(report)

    rag_restored = [
        m
        for m in restored
        if isinstance(m, ToolMessage) and m.name == "knowledge_base_search"
    ]
    report.check(
        "c1: checkpoint do runtime ANTIGO carregou com ToolMessage de RAG",
        len(rag_restored) == 1,
        f"rag_toolmessages={len(rag_restored)}",
    )
    report.check(
        "c4: ToolMessage(content=str) preservado byte-a-byte no replay",
        bool(rag_restored)
        and isinstance(rag_restored[0].content, str)
        and rag_restored[0].content == wrapped,
    )

    # c2: re-executar agent_node NOVO sem reprocessamento.
    print("[3] Re-executando agent_node NOVO a partir do checkpoint restaurado…")
    state = {
        "messages": restored,
        "company_id": "co-replay",
        "user_id": "user-replay",
        "session_id": "sess-replay",
        "company_config": {},
        "agent_data": {"id": "agent-replay", "llm_provider": "openai"},
        "system_prompt": "Você é um assistente prestativo.",
        "tools_used": [],
        "rag_chunks": [],
        "channel": "web",
    }
    llm = _RecordingLLM()
    out = await agent_node(state, config={}, llm_with_tools=llm)

    rag_sent = [
        m
        for m in (llm.seen_messages or [])
        if isinstance(m, ToolMessage) and m.name == "knowledge_base_search"
    ]
    report.check(
        "c2: agent_node chamou o LLM exatamente uma vez",
        llm.invocations == 1,
        f"invocations={llm.invocations}",
    )
    report.check(
        "c2: nenhuma busca RAG nova foi anexada (sem reprocessamento)",
        len(rag_sent) == 1,
        f"rag_toolmessages_enviadas={len(rag_sent)}",
    )
    expected_readable = json.loads(_legacy_rag_inner_json())["content"]
    report.check(
        "c2: conteúdo entregue ao LLM = campo legível DESEMBRULHADO do checkpoint",
        bool(rag_sent) and rag_sent[0].content == expected_readable,
        "unwrap do content ARMAZENADO (impossível sem o checkpoint)",
    )
    report.check(
        "c2: System prompt montado e resposta produzida sem disparar tool_node",
        bool(llm.seen_messages)
        and isinstance(llm.seen_messages[0], SystemMessage)
        and isinstance(out["messages"][0], AIMessage),
    )

    return _finish(report)


def _finish(report: Report) -> int:
    print("-" * 78)
    passed = sum(1 for _, ok, _ in report.checks if ok)
    total = len(report.checks)
    verdict = "PASS" if report.ok else "FAIL"
    print(f"RESULTADO: {verdict} ({passed}/{total} checks)")
    print("=" * 78)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
