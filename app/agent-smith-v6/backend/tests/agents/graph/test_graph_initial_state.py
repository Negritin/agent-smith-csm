"""
Testes de `graph._build_initial_state` — feat-037.

Provam que a construção do estado inicial:

- Deriva metadata do prompt EXCLUSIVAMENTE do ToolRegistry: chama
  `registry.get_discovery_snapshot(agent_id)` e
  `registry.get_prompt_metadata(agent_id, context)` (Registry mockado) — sem
  reconsultar agent_http_tools / agent_delegations / agent_mcp_tools nem fazer
  discovery duplicado.
- Injeta a metadata retornada no system prompt quando não vazia.
- NÃO altera o system prompt quando `get_prompt_metadata()` retorna vazio.
- Deriva allowed_http_tools a partir do snapshot do Registry.
- Monta o ToolExecutionContext de prompt com agent_id/session_id/company_id/
  user_id e is_subagent=False.

O Registry é substituído por um fake instrumentado via monkeypatch de
`app.agents.graph.get_tool_registry`, garantindo que a ÚNICA fonte de discovery
seja o Registry (nenhum acesso direto a banco neste helper).
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any, List, Tuple

from app.agents.runtime import DiscoverySnapshot, ToolExecutionContext

# --------------------------------------------------------------------------- #
# Import PREGUIÇOSO de graph.py
# --------------------------------------------------------------------------- #
# graph.py importa `from app.factories.llm_factory import LLMFactory` no topo.
# Importá-lo em tempo de COLLECTION carregaria llm_factory (real na CI, ou um stub)
# ANTES de tests/agents/tools/test_subagent_golden.py ser coletado — e aquele teste
# instala o PRÓPRIO stub de llm_factory condicionado a "ainda não estar em
# sys.modules". Para não pré-empcioná-lo, adiamos o import de graph.py para o
# tempo de EXECUÇÃO (depois de toda a collection), semeando a folha llm_factory via
# setdefault só se ninguém mais a tiver instalado.
_GRAPH_MODULE: Any = None


def _get_graph_module() -> Any:
    global _GRAPH_MODULE
    if _GRAPH_MODULE is None:
        if "app.factories.llm_factory" not in sys.modules:
            _mod = types.ModuleType("app.factories.llm_factory")

            class LLMFactory:  # pragma: no cover - graph tests não chamam create_llm
                @staticmethod
                def create_llm(*args: object, **kwargs: object) -> object:
                    return object()

            _mod.LLMFactory = LLMFactory  # type: ignore[attr-defined]
            sys.modules["app.factories.llm_factory"] = _mod

        from app.agents import graph as graph_module

        _GRAPH_MODULE = graph_module
    return _GRAPH_MODULE


# --------------------------------------------------------------------------- #
# Fake Registry instrumentado
# --------------------------------------------------------------------------- #
class _FakeRegistry:
    def __init__(self, snapshot: DiscoverySnapshot, metadata: str) -> None:
        self._snapshot = snapshot
        self._metadata = metadata
        self.snapshot_calls: List[str] = []
        self.prompt_calls: List[Tuple[str, ToolExecutionContext]] = []

    async def get_discovery_snapshot(self, agent_id: str) -> DiscoverySnapshot:
        self.snapshot_calls.append(agent_id)
        return self._snapshot

    async def get_prompt_metadata(
        self, agent_id: str, context: ToolExecutionContext
    ) -> str:
        self.prompt_calls.append((agent_id, context))
        return self._metadata

    def register_builder(self, *_args: Any, **_kwargs: Any) -> None:
        # register_default_builders() (chamado por _build_initial_state) itera os
        # builders de produção e invoca isto. No-op: o discovery é injetado direto
        # via snapshot/metadata. Sem este método a chamada estouraria
        # AttributeError — engolido pelo try/except do graph, mascarando o
        # discovery (snapshot_calls vazio) na suíte completa.
        pass


def _make_snapshot(
    *,
    agent_id: str = "agent-1",
    http_tools: Tuple[dict, ...] = (),
    ucp_connections: Tuple[dict, ...] = (),
) -> DiscoverySnapshot:
    return DiscoverySnapshot(
        agent_id=agent_id,
        fingerprint="fp",
        agent={"id": agent_id, "company_id": "company-1"},
        http_tools=http_tools,
        mcp_tools=(),
        mcp_connections=(),
        delegations=(),
        subagents=(),
        ucp_connections=ucp_connections,
    )


def _build(agent_id, registry, monkeypatch, **kwargs) -> tuple:
    graph_module = _get_graph_module()
    # O registro de builders padrão (adapters de produção) é irrelevante a estes
    # testes, que injetam o discovery via _FakeRegistry. Na suíte completa o
    # register_default_builders REAL pode ficar vinculado em graph e iterar os
    # builders chamando registry.register_builder — neutralizamos para isolar.
    monkeypatch.setattr(graph_module, "register_default_builders", lambda _r: None)
    if registry is not None:
        monkeypatch.setattr(graph_module, "get_tool_registry", lambda: registry)
    params = {
        "user_message": "Olá, o que é Flux Pay?",
        "company_id": "company-1",
        "user_id": "user-1",
        "session_id": "session-1",
        "company_config": {"company_name": "ACME"},
        "options": None,
        "supabase_client": None,
        "agent_id": agent_id,
        "channel": "web",
    }
    params.update(kwargs)
    return asyncio.run(graph_module._build_initial_state(**params))


# --------------------------------------------------------------------------- #
# 1. Registry é a fonte única do discovery + metadata injetada no prompt
# --------------------------------------------------------------------------- #
def test_build_initial_state_uses_registry_and_injects_metadata(monkeypatch) -> None:
    snapshot = _make_snapshot(http_tools=({"name": "create_ticket"},))
    registry = _FakeRegistry(snapshot, metadata="META_FROM_REGISTRY")

    initial_state, config, agent_data = _build("agent-1", registry, monkeypatch)

    # get_discovery_snapshot e get_prompt_metadata chamados com o agent_id certo.
    assert registry.snapshot_calls == ["agent-1"]
    assert len(registry.prompt_calls) == 1
    called_agent_id, called_context = registry.prompt_calls[0]
    assert called_agent_id == "agent-1"

    # O contexto de prompt carrega identidade canônica e is_subagent=False.
    assert isinstance(called_context, ToolExecutionContext)
    assert called_context.agent_id == "agent-1"
    assert called_context.session_id == "session-1"
    assert called_context.company_id == "company-1"
    assert called_context.user_id == "user-1"
    assert called_context.is_subagent is False
    assert called_context.allowed_http_tools == ["create_ticket"]

    # Metadata injetada no system prompt.
    assert "META_FROM_REGISTRY" in initial_state["system_prompt"]
    # allowed_http_tools derivado do snapshot do Registry.
    assert initial_state["allowed_http_tools"] == ["create_ticket"]
    # thread_id determinístico (company:session).
    assert config["configurable"]["thread_id"] == "company-1:session-1"


# --------------------------------------------------------------------------- #
# 2. Metadata vazia => system prompt NÃO é alterado
# --------------------------------------------------------------------------- #
def test_empty_metadata_does_not_change_system_prompt(monkeypatch) -> None:
    # Snapshot SEM http tools e SEM ucp => única diferença possível seria a
    # metadata. Com metadata vazia, o prompt deve ser idêntico ao baseline
    # construído sem agent_id (que pula o bloco do Registry por completo).
    snapshot = _make_snapshot(http_tools=(), ucp_connections=())
    registry = _FakeRegistry(snapshot, metadata="")

    with_registry, _, _ = _build("agent-1", registry, monkeypatch)

    # Baseline: agent_id=None NÃO chama o Registry (bloco inteiro é pulado).
    baseline, _, _ = _build(None, None, monkeypatch)

    assert registry.prompt_calls and registry.prompt_calls[0][0] == "agent-1"
    # Prompt idêntico: metadata vazia não acrescenta nada.
    assert with_registry["system_prompt"] == baseline["system_prompt"]
    assert with_registry["allowed_http_tools"] == []


# --------------------------------------------------------------------------- #
# 3. UCP/Commerce: instruções injetadas só quando há conexões UCP ativas
# --------------------------------------------------------------------------- #
def test_ucp_instructions_injected_when_ucp_connections_present(monkeypatch) -> None:
    snapshot = _make_snapshot(ucp_connections=({"id": "ucp-1"},))
    registry = _FakeRegistry(snapshot, metadata="")

    initial_state, _, _ = _build("agent-1", registry, monkeypatch)

    assert "SISTEMA DE COMMERCE (UCP)" in initial_state["system_prompt"]


def test_no_ucp_instructions_without_ucp_connections(monkeypatch) -> None:
    snapshot = _make_snapshot(ucp_connections=())
    registry = _FakeRegistry(snapshot, metadata="")

    initial_state, _, _ = _build("agent-1", registry, monkeypatch)

    assert "SISTEMA DE COMMERCE (UCP)" not in initial_state["system_prompt"]
