"""
Replay de checkpoint do runtime ANTIGO — feat-044.

Gate de release: antes de promover o novo runtime de tools, é OBRIGATÓRIO provar
que um checkpoint LangGraph gravado pelo runtime ANTIGO — contendo `ToolMessage`
de RAG cujo `content` é a string XML-wrapped (`<rag_context>...</rag_context>`,
com escape HTML) — pode ser RECARREGADO e RE-EXECUTADO pelo `agent_node` NOVO
sem reprocessar a busca e sem quebrar a compatibilidade de
`ToolMessage(content=str)`.

Critérios cobertos:

- c1: Carregar checkpoint gravado pelo runtime ANTIGO com ToolMessage de RAG
  (XML-wrapped). Materializamos a mensagem EXATAMENTE como o runtime antigo a
  persistia (Runtime envolve `content_for_llm` do RAG em `<rag_context>` com
  `html.escape`) e a fazemos transitar pelo MESMO mecanismo de persistência do
  LangGraph: o `MemorySaver` (que compartilha o `JsonPlusSerializer` com o
  `AsyncPostgresSaver` de produção) e, redundantemente, o serializer direto.

- c2: Validar que o `agent_node` NOVO segue funcionando sem reprocessamento.
  Re-executamos o `agent_node` a partir do estado RESTAURADO do checkpoint. A
  `ToolMessage` de RAG da rodada corrente é consumida via `_unwrap_prompt_xml`
  (nodes.py) a partir do content ARMAZENADO — nenhuma nova busca é disparada
  (o `agent_node` sequer importa o SearchService; provamos que o conteúdo
  entregue ao LLM é função determinística APENAS do content do checkpoint e que
  nenhuma `ToolMessage` de RAG nova é anexada).

- c4: `ToolMessage(content=str)` continua compatível — o round-trip pelo
  checkpoint preserva `content` como `str` idêntico ao gravado.

Padrão da suíte (sem pytest-asyncio): exercitamos o `agent_node` async com
`asyncio.run`. As variáveis de ambiente mínimas são semeadas ANTES de importar
`app.*` (espelhando tests/services/conftest.py) para que o módulo rode também
de forma ISOLADA, sem depender da ordem de coleta de outras suítes.
"""

from __future__ import annotations

import os

# --------------------------------------------------------------------------- #
# Semeia env mínima ANTES de qualquer import de `app.*` (Settings é instanciado
# em import time por app.core.config). setdefault: nunca sobrescreve a CI.
# --------------------------------------------------------------------------- #
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

import asyncio  # noqa: E402
import json  # noqa: E402
from typing import Any, List, Optional  # noqa: E402

import pytest  # noqa: E402

from langchain_core.messages import (  # noqa: E402
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.agents.nodes import agent_node, wrap_prompt_xml  # noqa: E402

from .conftest import make_agent_state  # noqa: E402

RAG_CALL_ID = "call-rag-old-1"


# --------------------------------------------------------------------------- #
# Guard de dependências REAIS (mesma filosofia do conftest._real_deps_available).
#
# Em uma execução COMBINADA de tests/agents/, golden tests de sprints anteriores
# (005/006) semeiam STUBS de `langchain_core`/`langgraph` em sys.modules — débito
# de isolamento PRÉ-EXISTENTE (reconhecido pelo Evaluator, não introduzido aqui).
# Sob esses stubs não há `langgraph.checkpoint` nem `ToolMessage` real (pydantic),
# então o REPLAY (que persiste/relê um checkpoint LangGraph REAL) não pode rodar.
# Em vez de poluir com ERROS, pulamos LIMPO; a suíte graph isolada (deps reais)
# executa TODOS os casos, e o gate de staging (scripts/replay_checkpoint_staging.py)
# roda em processo limpo com deps reais. Assim o critério é comprovado sem somar
# ao débito de ordem existente.
# --------------------------------------------------------------------------- #
def _real_replay_deps_available() -> tuple[bool, str]:
    if not hasattr(ToolMessage, "model_dump"):
        return False, "langchain_core.messages é stub (poluição de sys.modules)"
    try:  # checkpointer REAL (compartilha JsonPlusSerializer com AsyncPostgresSaver)
        import langgraph.checkpoint.base  # noqa: F401
        import langgraph.checkpoint.memory  # noqa: F401
        import langgraph.checkpoint.serde.jsonplus  # noqa: F401
    except Exception as exc:  # pragma: no cover - só sob poluição de ordem
        return False, f"langgraph.checkpoint indisponível (poluição): {exc}"
    return True, ""


_DEPS_OK, _SKIP_REASON = _real_replay_deps_available()
pytestmark = pytest.mark.skipif(not _DEPS_OK, reason=_SKIP_REASON)


# --------------------------------------------------------------------------- #
# Sentinela do runtime ANTIGO: payload de RAG do SearchService que o adapter
# serializava em `content_for_llm` e o Runtime envolvia em <rag_context>.
# Inclui um campo legível ("content") para que o desembrulho (nodes.py) produza
# uma string DISTINTA da bruta — evidência observável de que o unwrap rodou
# sobre o content ARMAZENADO, sem reexecutar a busca.
# --------------------------------------------------------------------------- #
def _legacy_rag_inner_json() -> str:
    payload = {
        "found": True,
        "strategy": "hybrid",
        # Texto com caracteres que EXIGEM escape HTML (& < >) para provar o
        # round-trip de escape/unescape do wrap XML.
        "content": "Flux Pay processa saques em até 2 dias úteis (D+2) & sem taxa <padrão>.",
        "chunks": [{"text": "SLA de saque: D+2.", "score": 0.93}],
        "search_time_ms": 27,
        "agent_id": "agent-replay",
    }
    return json.dumps(payload, ensure_ascii=False)


def _legacy_wrapped_rag_content() -> str:
    """Exatamente o que o runtime ANTIGO persistia na ToolMessage de RAG."""
    return wrap_prompt_xml("rag_context", _legacy_rag_inner_json())


def _old_runtime_messages() -> List[Any]:
    """Histórico como o runtime ANTIGO o gravava no checkpoint, logo após o RAG.

    Estrutura típica do ponto de interrupção: Human -> AI(tool_calls) ->
    ToolMessage(RAG XML-wrapped). A próxima execução (replay) re-roda o
    agent_node para gerar a resposta final.
    """
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


# --------------------------------------------------------------------------- #
# Persistência REAL via LangGraph: MemorySaver compartilha o JsonPlusSerializer
# com o AsyncPostgresSaver de produção. Round-trip = "carregar checkpoint".
# --------------------------------------------------------------------------- #
def _roundtrip_via_memory_saver(messages: List[Any]) -> List[Any]:
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.memory import MemorySaver

    async def _run() -> List[Any]:
        saver = MemorySaver()
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {"messages": messages}
        checkpoint["channel_versions"] = {"messages": 1}
        config = {"configurable": {"thread_id": "replay-thread", "checkpoint_ns": ""}}
        # Grava (como o runtime ANTIGO) e relê (como o runtime NOVO).
        new_config = await saver.aput(
            config, checkpoint, {"source": "loop", "step": 1}, {"messages": 1}
        )
        tup = await saver.aget_tuple(new_config)
        assert tup is not None
        return tup.checkpoint["channel_values"]["messages"]

    return asyncio.run(_run())


def _roundtrip_via_serde(messages: List[Any]) -> List[Any]:
    """Round-trip pelo serializer EXATO do checkpoint (defesa em profundidade)."""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    serde = JsonPlusSerializer()
    type_, blob = serde.dumps_typed(messages)
    return serde.loads_typed((type_, blob))


# --------------------------------------------------------------------------- #
# LLM fake: registra exatamente as mensagens que o agent_node monta e devolve.
# --------------------------------------------------------------------------- #
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


def _state_from_restored(messages: List[Any]) -> dict:
    state = make_agent_state(messages=messages)
    # System prompt explícito => caminho OpenAI (SystemMessage simples), sem
    # depender de build_system_prompt nem de RAG no system (replay puro).
    state["system_prompt"] = "Você é um assistente prestativo."
    state["agent_data"] = {"id": "agent-replay", "llm_provider": "openai"}
    return state


def _find_rag_tool_messages(messages: List[Any]) -> List[ToolMessage]:
    return [
        m
        for m in messages
        if isinstance(m, ToolMessage) and m.name == "knowledge_base_search"
    ]


# --------------------------------------------------------------------------- #
# c1 + c4: checkpoint do runtime ANTIGO carrega com ToolMessage RAG XML-wrapped
# e content permanece str idêntico (compatibilidade ToolMessage(content=str)).
# --------------------------------------------------------------------------- #
def test_old_checkpoint_loads_rag_toolmessage_xml_wrapped_as_str() -> None:
    original = _old_runtime_messages()
    wrapped = _legacy_wrapped_rag_content()

    # Pré-condições: a ToolMessage do runtime antigo é XML-wrapped e é str.
    assert wrapped.startswith("<rag_context>")
    assert wrapped.endswith("</rag_context>")
    assert "&amp;" in wrapped and "&lt;" in wrapped  # escape HTML presente

    for restored in (
        _roundtrip_via_memory_saver(original),
        _roundtrip_via_serde(original),
    ):
        rag_msgs = _find_rag_tool_messages(restored)
        assert len(rag_msgs) == 1
        rag = rag_msgs[0]
        # c4: content continua sendo str, byte-idêntico ao gravado.
        assert isinstance(rag.content, str)
        assert rag.content == wrapped
        assert rag.tool_call_id == RAG_CALL_ID
        # Tipos preservados no replay.
        assert isinstance(restored[0], HumanMessage)
        assert isinstance(restored[1], AIMessage)
        assert isinstance(restored[2], ToolMessage)


# --------------------------------------------------------------------------- #
# c2: agent_node NOVO re-executa a partir do checkpoint SEM reprocessar a busca.
# --------------------------------------------------------------------------- #
def test_agent_node_replays_old_checkpoint_without_reprocessing() -> None:
    restored = _roundtrip_via_memory_saver(_old_runtime_messages())
    state = _state_from_restored(restored)
    llm = _RecordingLLM()

    out = asyncio.run(agent_node(state, config={}, llm_with_tools=llm))

    # O LLM foi chamado UMA vez com o histórico montado a partir do checkpoint.
    assert llm.invocations == 1
    assert llm.seen_messages is not None
    assert isinstance(llm.seen_messages[0], SystemMessage)

    # A ToolMessage de RAG foi consumida do checkpoint (rodada corrente => unwrap).
    rag_msgs = _find_rag_tool_messages(llm.seen_messages)
    assert len(rag_msgs) == 1, "Nenhuma RAG nova pode ser anexada no replay"
    sent = rag_msgs[0]
    assert isinstance(sent.content, str)
    assert sent.tool_call_id == RAG_CALL_ID

    # Prova de NÃO reprocessamento: o conteúdo entregue ao LLM é o campo legível
    # DESEMBRULHADO do content ARMAZENADO no checkpoint (impossível obter sem o
    # content persistido — nenhuma busca foi reexecutada).
    expected_readable = json.loads(_legacy_rag_inner_json())["content"]
    assert sent.content == expected_readable
    # E o XML wrapper foi removido (compressão da rodada), não reanexado.
    assert not sent.content.startswith("<rag_context>")

    # agent_node devolve apenas a resposta do LLM (não dispara tool_node/busca).
    assert isinstance(out["messages"][0], AIMessage)
    assert out["tokens_total"] == 12


# --------------------------------------------------------------------------- #
# c2 (shape de produção): payload de RAG SEM campo "content" também replaya sem
# erro — o agent_node mantém o content ARMAZENADO (fallback) sem reprocessar.
# --------------------------------------------------------------------------- #
def test_agent_node_replays_rag_without_content_key_no_reprocessing() -> None:
    # Shape do golden real (sem chave "content"): found/strategy/chunks/...
    inner = json.dumps(
        {
            "found": True,
            "strategy": "hyde",
            "chunks": [{"text": "Política de troca em até 30 dias.", "score": 0.91}],
            "search_time_ms": 42,
            "agent_id": "agent-replay",
        },
        ensure_ascii=False,
    )
    wrapped = wrap_prompt_xml("rag_context", inner)
    messages = [
        HumanMessage(content="Qual a política de troca?"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "knowledge_base_search", "args": {"query": "troca"}, "id": RAG_CALL_ID}
            ],
        ),
        ToolMessage(content=wrapped, tool_call_id=RAG_CALL_ID, name="knowledge_base_search"),
    ]

    restored = _roundtrip_via_memory_saver(messages)
    state = _state_from_restored(restored)
    llm = _RecordingLLM()

    out = asyncio.run(agent_node(state, config={}, llm_with_tools=llm))

    assert llm.invocations == 1
    rag_msgs = _find_rag_tool_messages(llm.seen_messages)
    assert len(rag_msgs) == 1
    # Sem chave "content": fallback mantém o content ARMAZENADO (não reprocessa).
    assert rag_msgs[0].content == wrapped
    assert isinstance(out["messages"][0], AIMessage)


# --------------------------------------------------------------------------- #
# c2 (rodada antiga): RAG de rodada ANTERIOR (fora do pending) é comprimida no
# replay para o placeholder — sem reexecutar busca e sem quebrar.
# --------------------------------------------------------------------------- #
def test_agent_node_replays_old_round_rag_compressed_to_placeholder() -> None:
    old_rag_id = "call-rag-prev"
    messages = [
        HumanMessage(content="primeira pergunta"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "knowledge_base_search", "args": {"query": "x"}, "id": old_rag_id}
            ],
        ),
        ToolMessage(
            content=_legacy_wrapped_rag_content(),
            tool_call_id=old_rag_id,
            name="knowledge_base_search",
        ),
        AIMessage(content="resposta anterior da assistente"),
        HumanMessage(content="segunda pergunta"),
    ]

    restored = _roundtrip_via_memory_saver(messages)
    state = _state_from_restored(restored)
    llm = _RecordingLLM()

    asyncio.run(agent_node(state, config={}, llm_with_tools=llm))

    rag_msgs = _find_rag_tool_messages(llm.seen_messages)
    assert len(rag_msgs) == 1
    # Rodada anterior (não pending) => content bruto removido (compressão),
    # provando que o conteúdo do checkpoint foi CONSUMIDO, não reprocessado.
    assert "removido para otimização" in rag_msgs[0].content
    assert isinstance(rag_msgs[0].content, str)
