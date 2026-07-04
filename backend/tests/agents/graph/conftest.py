"""
Conftest dos testes de grafo/nodes (feat-037).

Os módulos sob teste — `app.agents.nodes` (tool_node, _build_tool_context,
_build_initial_state via graph) e `app.agents.graph` — dependem, em import time,
de bibliotecas pesadas (langchain_core, langgraph) e de módulos de serviço que
abrem conexões reais (Supabase, LLM factories, memory service). Estes testes
exercitam a LÓGICA do tool_node e do _build_initial_state em isolamento; eles
NÃO devem exigir as dependências de produção instaladas.

Para manter a suíte hermética em qualquer ambiente, este conftest semeia
`sys.modules` (via setdefault — nunca sobrescreve dependências reais já
disponíveis na CI) com:

1. Stubs mínimos de `langchain_core` (BaseTool, messages, runnables).
2. Stubs de `langgraph.graph` (END/START/StateGraph) e `langgraph.graph.message`
   (add_messages) — usados em import time por graph.py e state.py.
3. Pacote sintético `app.agents` apontando para o diretório REAL, evitando
   executar `app/agents/__init__.py` (que importa o grafo completo, puxando
   langchain). Os submódulos reais (nodes, graph, state, utils, runtime) são
   carregados a partir do disco via __path__.
4. Stub de `app.agents.tool_builders` (build_available_subagents_map,
   register_default_builders) — o módulo real importa TODOS os adapters
   concretos (heavy). Os testes mockam o Registry, então o builder real é
   desnecessário.
5. Stubs dos módulos de serviço importados no topo de graph.py / nodes.py
   (prompts, utils, llm_factory, agent_service, memory_service, constants,
   llama_guard_service, database).

O ToolRegistry e os contratos do runtime (`app.agents.runtime.*`) são REAIS:
os testes E2E do tool_node executam SEMPRE via `registry.execute_tool`.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _ensure_backend_on_path() -> None:
    backend = str(_BACKEND_ROOT)
    if backend not in sys.path:
        sys.path.insert(0, backend)


def _register(name: str, module: types.ModuleType) -> types.ModuleType:
    sys.modules.setdefault(name, module)
    return sys.modules[name]


def _make_module(name: str, **attrs: object) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return _register(name, module)


def _make_package(name: str, search_path: pathlib.Path | None = None) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        module.__path__ = [str(search_path)] if search_path is not None else []
        module.__package__ = name
    return _register(name, module)


# --------------------------------------------------------------------------- #
# 1. langchain_core (tools.BaseTool + messages + runnables).
# --------------------------------------------------------------------------- #
def _install_langchain_stub() -> None:
    if "langchain_core.tools" not in sys.modules:
        class _StubBaseTool(BaseModel):
            model_config = ConfigDict(arbitrary_types_allowed=True)

            name: str = ""
            description: str = ""
            args_schema: object = None

        lc = _make_package("langchain_core")
        tools = _make_module("langchain_core.tools", BaseTool=_StubBaseTool)
        setattr(lc, "tools", tools)

    if "langchain_core.messages" not in sys.modules:
        class _Msg:
            def __init__(self, content: Any = "", **kwargs: Any) -> None:
                self.content = content
                for key, value in kwargs.items():
                    setattr(self, key, value)

        class SystemMessage(_Msg):
            type = "system"

        class HumanMessage(_Msg):
            type = "human"

        class AIMessage(_Msg):
            type = "ai"

            def __init__(self, content: Any = "", **kwargs: Any) -> None:
                # tool_calls default vazio para espelhar o LangChain real.
                kwargs.setdefault("tool_calls", [])
                super().__init__(content=content, **kwargs)

        class ToolMessage(_Msg):
            type = "tool"

            def __init__(
                self,
                content: Any = "",
                tool_call_id: Optional[str] = None,
                name: Optional[str] = None,
                **kwargs: Any,
            ) -> None:
                super().__init__(
                    content=content,
                    tool_call_id=tool_call_id,
                    name=name,
                    **kwargs,
                )

        _make_module(
            "langchain_core.messages",
            SystemMessage=SystemMessage,
            HumanMessage=HumanMessage,
            AIMessage=AIMessage,
            ToolMessage=ToolMessage,
        )

    if "langchain_core.runnables" not in sys.modules:
        _make_module("langchain_core.runnables", RunnableConfig=dict)


# --------------------------------------------------------------------------- #
# 2. langgraph (graph + message).
# --------------------------------------------------------------------------- #
def _install_langgraph_stub() -> None:
    if "langgraph.graph" in sys.modules:
        return

    _make_package("langgraph")

    class _StateGraph:  # pragma: no cover - usado só por create_agent_graph
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.nodes: Dict[str, Any] = {}

        def add_node(self, *args: object, **kwargs: object) -> None:
            pass

        def add_edge(self, *args: object, **kwargs: object) -> None:
            pass

        def add_conditional_edges(self, *args: object, **kwargs: object) -> None:
            pass

        def compile(self, *args: object, **kwargs: object) -> object:
            return object()

    graph_mod = _make_module(
        "langgraph.graph",
        END="__END__",
        START="__START__",
        StateGraph=_StateGraph,
    )

    def add_messages(left: Any, right: Any) -> Any:  # pragma: no cover
        return (left or []) + (right or [])

    msg_mod = _make_module("langgraph.graph.message", add_messages=add_messages)
    setattr(graph_mod, "message", msg_mod)


# --------------------------------------------------------------------------- #
# 3. Pacote sintético app.agents (aponta para o diretório real).
# --------------------------------------------------------------------------- #
def _install_agents_package() -> None:
    import app  # noqa: F401  (pacote real e leve)

    agents_path = _BACKEND_ROOT / "app" / "agents"
    _make_package("app.agents", agents_path)


# --------------------------------------------------------------------------- #
# 4. Stub de app.agents.tool_builders (real importa adapters pesados).
# --------------------------------------------------------------------------- #
def _install_tool_builders_stub() -> None:
    def build_available_subagents_map(snapshot: Any) -> dict:
        sub_map = {str(s.get("id")): s for s in getattr(snapshot, "subagents", ())}
        result: dict = {}
        for delegation in getattr(snapshot, "delegations", ()):
            sub_id = delegation.get("subagent_id")
            sub_data = sub_map.get(str(sub_id)) if sub_id else None
            if not sub_data:
                continue
            result[str(sub_id)] = {
                "subagent_data": sub_data,
                "task_description": delegation.get("task_description"),
                "max_context_chars": delegation.get("max_context_chars", 2000),
                "timeout_seconds": delegation.get("timeout_seconds", 30),
                "max_iterations": delegation.get("max_iterations", 5),
            }
        return result

    def register_default_builders(registry: Any) -> None:
        # No-op: os testes mockam o Registry ou usam tools fake explícitas.
        return None

    _make_module(
        "app.agents.tool_builders",
        build_available_subagents_map=build_available_subagents_map,
        register_default_builders=register_default_builders,
    )


# --------------------------------------------------------------------------- #
# 5. Stubs dos módulos de serviço / core importados em import time.
# --------------------------------------------------------------------------- #
def _install_service_stubs() -> None:
    _make_package("app.core")
    _make_package("app.factories")
    _make_package("app.services")

    if "app.core.constants" not in sys.modules:
        _make_module("app.core.constants", AGENT_CONTEXT_WINDOW_SIZE=15)

    if "app.core.prompts" not in sys.modules:
        def build_composite_prompt(
            base_prompt: str = "", client_instructions: str = "", *args: object, **kwargs: object
        ) -> str:
            # Identidade: preserva client_instructions (= base_instructions do graph,
            # agora o 2º arg após o base_prompt dinâmico) para que os testes possam
            # asserir a injeção (ou não) da metadata do Registry no prompt.
            return client_instructions

        _make_module("app.core.prompts", build_composite_prompt=build_composite_prompt)

    if "app.services.platform_settings_service" not in sys.modules:
        async def get_system_base_prompt() -> str:
            # Stub: base prompt dinâmico vazio nos testes do graph (não bate em Redis/DB).
            return ""

        _make_module(
            "app.services.platform_settings_service",
            get_system_base_prompt=get_system_base_prompt,
        )

    if "app.core.utils" not in sys.modules:
        def get_api_key_for_provider(provider: str) -> str:
            return "fake-api-key"

        # normalize_phone é importada por outros testes (whatsapp/webhook); injeta a
        # impl REAL (utils.py é leve) para o stub não poluir o sys.modules quebrando
        # aqueles imports na suíte inteira (B2.1).
        import importlib.util as _ilu
        from pathlib import Path as _P

        _spec = _ilu.spec_from_file_location(
            "_app_core_utils_real_g",
            _P(__file__).resolve().parents[3] / "app" / "core" / "utils.py",
        )
        _real = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_real)
        _make_module(
            "app.core.utils",
            get_api_key_for_provider=get_api_key_for_provider,
            normalize_phone=_real.normalize_phone,
        )

    # NOTA: `app.factories.llm_factory` é deliberadamente NÃO instalado aqui.
    # O golden test do SubAgent (tests/agents/tools/test_subagent_golden.py)
    # instala o PRÓPRIO stub de llm_factory (com holder configurável) no topo do
    # módulo, condicionado a `if "app.factories.llm_factory" not in sys.modules`.
    # Se este conftest (carregado ANTES, ordem alfabética graph < tools) semeasse
    # um llm_factory devolvendo object(), pré-empcionaria aquele stub e quebraria
    # os testes do SubAgent. Por isso o stub de llm_factory é instalado de forma
    # PREGUIÇOSA, em tempo de execução, só pelo test_graph_initial_state (que é o
    # único que importa graph.py e, portanto, precisa do símbolo no import).

    if "app.services.agent_service" not in sys.modules:
        class AgentService:
            def get_agent_by_id(self, agent_id: str) -> Any:
                return None

        _make_module("app.services.agent_service", AgentService=AgentService)

    if "app.services.memory_service" not in sys.modules:
        class MemoryService:  # pragma: no cover - só usado quando há supabase_client
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

        _make_module("app.services.memory_service", MemoryService=MemoryService)

    if "app.core.database" not in sys.modules:
        def get_supabase_client(*args: object, **kwargs: object) -> object:
            raise RuntimeError(
                "get_supabase_client stub não deve ser usado nos testes de grafo."
            )

        _make_module("app.core.database", get_supabase_client=get_supabase_client)


# --------------------------------------------------------------------------- #
# 6. Stub de app.services.llama_guard_service (SEMPRE — inclusive na CI).
# --------------------------------------------------------------------------- #
def _install_llama_guard_stub() -> None:
    """Instala APENAS o módulo-folha llama_guard_service (sem sombrear o pacote).

    `enforce_prompt_safety` (nodes.py) importa este módulo PREGUIÇOSAMENTE e chama
    `get_llama_guard_service().validate_all(...)`, que em produção faz uma chamada
    de rede ao LlamaGuard. Mesmo na CI (deps reais), os testes de _build_initial_state
    NÃO podem disparar essa chamada — por isso semeamos só a folha via setdefault,
    deixando o pacote real `app.services` intacto para as suítes irmãs.
    """
    if "app.services.llama_guard_service" not in sys.modules:
        class _FakeLlamaGuard:
            async def validate_all(self, *args: object, **kwargs: object):
                # (is_unsafe, reason) — sempre seguro nos testes.
                return False, ""

        def get_llama_guard_service() -> _FakeLlamaGuard:
            return _FakeLlamaGuard()

        _make_module(
            "app.services.llama_guard_service",
            get_llama_guard_service=get_llama_guard_service,
        )


# Dependências pesadas (langchain_core/langgraph). Quando REAIS estão disponíveis
# (CI/Evaluator), NÃO instalamos os stubs: em uma execução combinada de
# `tests/agents`, a suíte `runtime/` importa o langchain real ANTES e os testes de
# shape do shim (ex.: test_bind_tools_compatibility_shape) exigem o BaseTool real.
# Semear o stub aqui (graph < runtime na ordem alfabética) pré-empcionaria o real
# e quebraria aquelas suítes.
_REAL_DEP_SPECS = (
    "langchain_core",
    "langchain_core.tools",
    "langchain_core.messages",
    "langchain_core.runnables",
    "langgraph.graph",
)


def _real_deps_available() -> bool:
    """True quando todas as dependências reais são importáveis (ambiente CI)."""
    for spec_name in _REAL_DEP_SPECS:
        try:
            if importlib.util.find_spec(spec_name) is None:
                return False
        except (ImportError, ValueError, ModuleNotFoundError):
            return False
    return True


def _bootstrap() -> None:
    _ensure_backend_on_path()
    # SEMPRE: pacote sintético app.agents (evita rodar app/agents/__init__.py, que
    # importaria graph.py -> llm_factory já no COLLECTION e pré-empcionaria o stub
    # de llm_factory do SubAgent). Submódulos reais (nodes/graph/state/runtime) são
    # carregados do disco via __path__.
    _install_agents_package()
    # SEMPRE: tool_builders stub (o real importa adapters pesados) e a folha do
    # llama_guard (chamada de rede). Ambos via setdefault — seguros p/ suítes irmãs.
    _install_tool_builders_stub()
    _install_llama_guard_stub()
    # Só no fallback LOCAL (sem deps reais): stubs de langchain/langgraph e dos
    # módulos de serviço/core importados em import time por graph.py. Na CI, esses
    # são reais e importáveis — não devemos sombreá-los.
    if not _real_deps_available():
        _install_langchain_stub()
        _install_langgraph_stub()
        _install_service_stubs()


_bootstrap()


# --------------------------------------------------------------------------- #
# Helpers compartilhados pelos testes (fakes de AgentTool + args schema).
# --------------------------------------------------------------------------- #
class EchoArgs(BaseModel):
    query: str = ""


def make_agent_state(**overrides: Any) -> dict:
    """AgentState mínimo para exercitar o tool_node."""
    state: Dict[str, Any] = {
        "messages": [],
        "company_id": "company-1",
        "user_id": "user-1",
        "session_id": "session-1",
        "company_config": {},
        "agent_data": {"id": "agent-1", "is_hyde_enabled": True},
        "tools_used": [],
        "rag_chunks": [],
        "rag_search_time_ms": 0,
        "internal_steps": [],
        "tool_raw_logs": [],
        "tokens_input": 0,
        "tokens_output": 0,
        "tokens_total": 0,
        "available_subagents": {},
        "channel": "web",
    }
    state.update(overrides)
    return state
