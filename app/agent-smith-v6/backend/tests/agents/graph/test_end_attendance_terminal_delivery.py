"""S5 (§10.2 itens 5/6) — entrega EFETIVA da mensagem final no turno terminal.

[validador] Não basta suprimir a 2ª geração do LLM: a mensagem final controlada
EXCLUSIVAMENTE pela tool (``end_attendance`` com ``send_closing_message=true``)
precisa ser ENTREGUE ao cliente no turno terminal — em AMBOS os caminhos:

  - AGREGADO (``invoke_agent`` → WhatsApp): lê ``state.final_response`` e o
    devolve como a resposta do turno.
  - STREAMING (``stream_agent`` → chat web/widget via SSE): no turno terminal o
    grafo roteia tools → log/END SEM voltar ao nó ``agent``, então o loop de
    ``astream_events`` não emite nenhum token. A correção lê o estado final
    (``graph.aget_state``) e emite ``final_response`` como a única saída.

Estes testes provam o CONTEÚDO entregue (não só o state field), exercitando as
funções de produção com um grafo fake — cobrindo o gap que o teste de roteamento
(nível de nó) deixava mascarado.

Sem pytest-asyncio: async via asyncio.run (padrão das suítes do grafo). Import
preguiçoso de graph.py (mesmo motivo de test_graph_initial_state).
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any, Dict, List, Optional

TERMINAL_MESSAGE = "Fico feliz em ter ajudado! Encerrando por aqui. 👋"


# --------------------------------------------------------------------------- #
# Import PREGUIÇOSO de graph.py (espelha test_graph_initial_state)
# --------------------------------------------------------------------------- #
_GRAPH_MODULE: Any = None


def _seed_llm_factory_stub() -> None:
    if "app.factories.llm_factory" not in sys.modules:
        _mod = types.ModuleType("app.factories.llm_factory")

        class LLMFactory:  # pragma: no cover
            @staticmethod
            def create_llm(*args: object, **kwargs: object) -> object:
                return object()

        _mod.LLMFactory = LLMFactory  # type: ignore[attr-defined]
        sys.modules["app.factories.llm_factory"] = _mod


def _seed_heavy_service_leaf_stubs() -> None:
    """Fallback LOCAL: stub das folhas pesadas importadas no topo de graph.py.

    Só é acionado quando o import real falha (ex.: ambientes locais com
    ``app.services.__init__`` puxando deps nativas quebradas). Usa ``setdefault``
    para NUNCA sombrear módulos reais já importados em CI.
    """
    if "app.services.agent_service" not in sys.modules:
        _svc = types.ModuleType("app.services.agent_service")

        class AgentService:  # pragma: no cover
            def get_agent_by_id(self, agent_id: str) -> Any:
                return None

        _svc.AgentService = AgentService  # type: ignore[attr-defined]
        sys.modules.setdefault("app.services.agent_service", _svc)

    if "app.services.memory_core" not in sys.modules:
        _mc = types.ModuleType("app.services.memory_core")

        def should_summarize(*args: object, **kwargs: object) -> bool:  # pragma: no cover
            return False

        _mc.should_summarize = should_summarize  # type: ignore[attr-defined]
        sys.modules.setdefault("app.services.memory_core", _mc)

    if "app.services.memory_service" not in sys.modules:
        _ms = types.ModuleType("app.services.memory_service")

        class MemoryService:  # pragma: no cover
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

        _ms.MemoryService = MemoryService  # type: ignore[attr-defined]
        sys.modules.setdefault("app.services.memory_service", _ms)


def _get_graph_module() -> Any:
    global _GRAPH_MODULE
    if _GRAPH_MODULE is None:
        _seed_llm_factory_stub()
        try:
            from app.agents import graph as graph_module
        except Exception:
            # Fallback local: seed das folhas pesadas e nova tentativa.
            _seed_heavy_service_leaf_stubs()
            from app.agents import graph as graph_module

        _GRAPH_MODULE = graph_module
    return _GRAPH_MODULE


# --------------------------------------------------------------------------- #
# Grafo fake: turno terminal (agent é pulado após a tool)
# --------------------------------------------------------------------------- #
class _StateView:
    def __init__(self, values: Dict[str, Any]) -> None:
        self.values = values


class _FakeTerminalGraph:
    """Simula o turno terminal: astream_events NÃO emite token do nó `agent`.

    No turno terminal o roteamento é tools → log/END; o nó `agent` não roda, logo
    nenhum ``on_chat_model_stream`` do `agent` é produzido. ``ainvoke`` devolve o
    state final com ``final_response`` setado pela tool; ``aget_state`` devolve a
    mesma view (consumida pela entrega terminal do streaming).
    """

    def __init__(self, final_state: Dict[str, Any], stream_events: Optional[List[dict]] = None) -> None:
        self._final_state = final_state
        self._stream_events = stream_events or []

    async def ainvoke(self, initial_state, config):
        return self._final_state

    async def astream_events(self, initial_state, config, version="v2"):
        for event in self._stream_events:
            yield event

    async def aget_state(self, config):
        return _StateView(self._final_state)


def _patch_build_initial_state(graph_module, monkeypatch) -> None:
    async def _fake_build(*args: Any, **kwargs: Any):
        initial_state: Dict[str, Any] = {"messages": []}
        config = {"configurable": {"thread_id": "co-1:s-1"}}
        return initial_state, config, {"id": "a-1"}

    monkeypatch.setattr(graph_module, "_build_initial_state", _fake_build)
    # LangSmith desligado e silencioso nos testes. `is_langsmith_enabled` é
    # importado DENTRO de invoke_agent/stream_agent (import local), então
    # patchamos no módulo de origem para o efeito alcançar o import local.
    import app.core.langsmith_setup as ls_setup

    monkeypatch.setattr(ls_setup, "is_langsmith_enabled", lambda: False, raising=False)


def _terminal_state() -> Dict[str, Any]:
    return {
        "messages": [],
        "attendance_terminal": True,
        "attendance_terminal_reason": "pedido_resolvido",
        "final_response": TERMINAL_MESSAGE,
    }


# =========================================================================== #
# AGGREGATE (invoke_agent) entrega final_response
# =========================================================================== #
def test_invoke_agent_delivers_terminal_final_response(monkeypatch) -> None:
    graph_module = _get_graph_module()
    _patch_build_initial_state(graph_module, monkeypatch)

    fake_graph = _FakeTerminalGraph(final_state=_terminal_state())

    result = asyncio.run(
        graph_module.invoke_agent(
            fake_graph,
            user_message="obrigado!",
            company_id="co-1",
            user_id="u-1",
            session_id="s-1",
            company_config={},
            agent_id="a-1",
        )
    )
    # A resposta agregada do turno terminal É a mensagem da tool (entrega efetiva).
    # invoke_agent devolve o dict canônico do turno; a saída ao cliente é "response".
    assert result["response"] == TERMINAL_MESSAGE


# =========================================================================== #
# STREAMING (stream_agent) entrega final_response como única saída do turno
# =========================================================================== #
def test_stream_agent_delivers_terminal_final_response(monkeypatch) -> None:
    graph_module = _get_graph_module()
    _patch_build_initial_state(graph_module, monkeypatch)

    # astream_events NÃO emite token do nó `agent` (turno terminal).
    fake_graph = _FakeTerminalGraph(final_state=_terminal_state(), stream_events=[])

    async def _collect() -> List[Any]:
        out: List[Any] = []
        async for piece in graph_module.stream_agent(
            fake_graph,
            user_message="obrigado!",
            company_id="co-1",
            user_id="u-1",
            session_id="s-1",
            company_config={},
            agent_id="a-1",
        ):
            out.append(piece)
        return out

    pieces = asyncio.run(_collect())
    # Só tokens de texto (str) contam como saída ao cliente; status dicts não.
    text_pieces = [p for p in pieces if isinstance(p, str)]
    # CONTEÚDO entregue == mensagem final da tool (entrega efetiva no streaming).
    assert "".join(text_pieces) == TERMINAL_MESSAGE


# =========================================================================== #
# STREAMING: se o agente JÁ streamou texto, não duplica a despedida
# =========================================================================== #
def test_stream_agent_no_double_emit_when_already_streamed(monkeypatch) -> None:
    graph_module = _get_graph_module()
    _patch_build_initial_state(graph_module, monkeypatch)

    # Evento de token do nó `agent` (turno NÃO terminal: streamou texto normal).
    class _Chunk:
        content = "Olá! "

    streamed_event = {
        "event": "on_chat_model_stream",
        "name": "ChatModel",
        "metadata": {"langgraph_node": "agent"},
        "data": {"chunk": _Chunk()},
    }
    # Estado NÃO terminal: a entrega terminal não deve disparar.
    non_terminal_state = {"messages": [], "final_response": None}
    fake_graph = _FakeTerminalGraph(
        final_state=non_terminal_state, stream_events=[streamed_event]
    )

    async def _collect() -> List[Any]:
        out: List[Any] = []
        async for piece in graph_module.stream_agent(
            fake_graph,
            user_message="oi",
            company_id="co-1",
            user_id="u-1",
            session_id="s-1",
            company_config={},
            agent_id="a-1",
        ):
            out.append(piece)
        return out

    pieces = asyncio.run(_collect())
    text_pieces = [p for p in pieces if isinstance(p, str)]
    # Só o token streamado normalmente; nada de despedida extra.
    assert "".join(text_pieces) == "Olá! "
