"""S5 (§10.2/§22 item 2) — materialização de end_attendance via tools_config.

[validador] Prova o contrato de materialização das tools de atendimento no
``_build_core_tools`` (tool_builders), a partir do espelho em
``agent.tools_config``:

  - ``end_attendance`` SÓ é materializada quando
    ``tools_config.end_attendance.enabled == true`` (default false ⇒ não muda o
    comportamento atual do agente).
  - ``human_handoff`` continua materializando a tool por
    ``tools_config.human_handoff.enabled`` (§22 item 2).
  - Ligar/desligar a flag no snapshot muda o conjunto de tools materializadas — é
    o que dá lastro à invalidação do cache do ToolRegistry (o fingerprint observa
    ``agents``, onde o espelho vive; a invalidação real é coberta por
    integração que semeia ``agents.tools_config`` + bump de ``updated_at``).

Hermético: usa os stubs do conftest desta suíte (langchain + serviços).

IMPORTANTE (coleta da suíte completa): o conftest de ``tests/agents/graph`` é
coletado ANTES desta suíte (ordem alfabética) e semeia em ``sys.modules`` um STUB
de ``app.agents.tool_builders`` que expõe apenas ``build_available_subagents_map``
(o real importa adapters pesados). Um ``from app.agents.tool_builders import
_build_core_tools`` resolveria contra esse stub e levantaria
``ImportError: cannot import name '_build_core_tools'``, ABORTANDO a coleta de
``pytest tests/agents`` inteira. Para ficar imune à ordem de conftests irmãos,
carregamos o módulo REAL diretamente do arquivo (sob um nome privado), sem tocar
em ``sys.modules['app.agents.tool_builders']`` — os stubs do conftest desta suíte
(langchain_core.tools/messages/runnables + serviços + database) já cobrem as deps
de import de ``tool_builders`` (incluindo ``.tools.subagent_tool``), de modo que
``pytest tests/agents/tools`` coleta sem depender da ordem dos conftests irmãos.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

from app.agents.runtime.registry import DiscoverySnapshot

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[3]
_TOOL_BUILDERS_PATH = _BACKEND_ROOT / "app" / "agents" / "tool_builders.py"


def _load_real_build_core_tools():
    """Carrega ``_build_core_tools`` do arquivo real, contornando o stub irmão.

    Não registramos o módulo em ``sys.modules['app.agents.tool_builders']`` para
    não interferir com outras suítes (o stub do graph deve continuar valendo lá);
    usamos um nome privado dedicado a este teste.
    """
    # Nome FILHO de ``app.agents`` (não o ``tool_builders`` real, que está
    # stubado em sys.modules pela suíte irmã ``graph``) para que as importações
    # relativas do módulo (``from .runtime import ...``) resolvam contra o pacote
    # real ``app.agents`` (cujo ``__path__`` já é o diretório real nesta suíte).
    name = "app.agents._tool_builders_real_for_end_attendance_test"
    spec = importlib.util.spec_from_file_location(name, _TOOL_BUILDERS_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module._build_core_tools


_build_core_tools = _load_real_build_core_tools()


def _snapshot(tools_config: dict) -> DiscoverySnapshot:
    return DiscoverySnapshot(
        agent_id="a-1",
        fingerprint="fp",
        agent={
            "id": "a-1",
            "company_id": "co-1",
            "tools_config": tools_config,
        },
        http_tools=(),
        mcp_tools=(),
        mcp_connections=(),
        delegations=(),
        subagents=(),
        ucp_connections=(),
    )


def _tool_names(tools) -> set:
    return {getattr(t, "name", None) for t in tools}


def test_end_attendance_not_materialized_by_default() -> None:
    tools = _build_core_tools("a-1", _snapshot({}))
    assert "end_attendance" not in _tool_names(tools)


def test_end_attendance_materialized_when_enabled() -> None:
    tools = _build_core_tools(
        "a-1", _snapshot({"end_attendance": {"enabled": True}})
    )
    assert "end_attendance" in _tool_names(tools)


def test_human_handoff_still_materialized_when_enabled() -> None:
    tools = _build_core_tools(
        "a-1", _snapshot({"human_handoff": {"enabled": True}})
    )
    assert "request_human_agent" in _tool_names(tools)


def test_both_tools_independent() -> None:
    tools = _build_core_tools(
        "a-1",
        _snapshot(
            {
                "human_handoff": {"enabled": True},
                "end_attendance": {"enabled": True},
            }
        ),
    )
    names = _tool_names(tools)
    assert "request_human_agent" in names
    assert "end_attendance" in names


def test_toggle_changes_materialized_set() -> None:
    off = _tool_names(_build_core_tools("a-1", _snapshot({})))
    on = _tool_names(
        _build_core_tools("a-1", _snapshot({"end_attendance": {"enabled": True}}))
    )
    # A diferença é exatamente a tool end_attendance (lastro p/ invalidação).
    assert "end_attendance" in (on - off)
