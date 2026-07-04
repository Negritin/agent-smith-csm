"""
Conftest dos golden/equivalence tests dos Adapters (feat-027).

Os Adapters (app.agents.tools.*) e o Tool Runtime (app.agents.runtime.*)
dependem, em import time, de bibliotecas pesadas (langchain_core, httpx) e de
módulos de serviço que abrem conexões reais (Qdrant, Tavily, Supabase). Os
golden tests provam a PARIDADE da string da ToolMessage (content_for_llm) em
isolamento, injetando providers fake nos Adapters — eles NÃO devem exigir as
dependências de produção instaladas.

Para garantir que a suíte rode de forma hermética em qualquer ambiente, este
conftest semeia `sys.modules` com:

1. Stubs mínimos de `langchain_core` / `langchain_core.tools.BaseTool`
   (usados apenas na definição do LangChainToolShim, nunca exercitado aqui).
2. Stub de `httpx` (anotações de tipo em http_request.py são avaliadas em def
   time).
3. Stubs dos módulos de serviço importados no topo de cada Adapter
   (search_service, qdrant_service, tavily_service, filesystem_search_service)
   e do `app.core.security.url_validator`. As funções `get_*` existem apenas
   para satisfazer o import; cada teste injeta seu próprio provider fake.
4. Pacotes sintéticos `app.agents` e `app.agents.tools` apontando para o
   diretório real, evitando executar os `__init__.py` que importam o grafo
   completo (graph.py -> langchain) e fábricas pesadas (mcp/ucp/subagent).

Isso mantém os testes focados na lógica do Adapter, exatamente como exige o
critério INEGOCIÁVEL de golden tests por adapter.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _real_dep_available(spec_name: str) -> bool:
    """True quando a dependência real é importável (CI / venv completo)."""
    try:
        return importlib.util.find_spec(spec_name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


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
# 1. Stub de langchain_core (BaseTool + messages + runnables).
# --------------------------------------------------------------------------- #
def _install_langchain_stub() -> None:
    # Mesmo gate de tests/agents/graph/conftest.py: com a dependência REAL
    # importável (CI / venv completo), NÃO sombrear — suítes irmãs coletadas
    # depois (ex.: tests/agents/runtime) dependem do BaseTool real (.args).
    #
    # Este conftest precisa cobrir, por conta própria, TODAS as deps de import
    # de tool_builders.py (carregado via exec_module por
    # test_end_attendance_materialization.py). Além de `langchain_core.tools`,
    # tool_builders -> .tools.subagent_tool importa `langchain_core.messages`
    # (e .runnables é dep comum dos adapters). Sem estes stubs, rodar
    # `pytest tests/agents/tools` SOZINHO (sem a suíte graph/ semear os stubs
    # antes) quebrava com ModuleNotFoundError. Espelha graph/conftest.py.
    if "langchain_core.tools" not in sys.modules and not _real_dep_available(
        "langchain_core.tools"
    ):
        class _StubBaseTool(BaseModel):
            """BaseTool mínimo compatível com a definição do LangChainToolShim."""

            model_config = ConfigDict(arbitrary_types_allowed=True)

            name: str = ""
            description: str = ""
            args_schema: object = None

        lc = _make_package("langchain_core")
        tools = _make_module("langchain_core.tools", BaseTool=_StubBaseTool)
        setattr(lc, "tools", tools)

    if "langchain_core.messages" not in sys.modules and not _real_dep_available(
        "langchain_core.messages"
    ):
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

    if "langchain_core.runnables" not in sys.modules and not _real_dep_available(
        "langchain_core.runnables"
    ):
        _make_module("langchain_core.runnables", RunnableConfig=dict)


# --------------------------------------------------------------------------- #
# 2. Stub de httpx (usado em anotações de http_request.py).
# --------------------------------------------------------------------------- #
def _install_httpx_stub() -> None:
    if "httpx" in sys.modules:
        return
    if _real_dep_available("httpx"):
        return

    class _Response:  # noqa: D401 - placeholder de tipo
        pass

    class _AsyncClient:  # pragma: no cover - nunca instanciado nos golden tests
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("httpx stub não deve ser usado nos golden tests")

    class _Client:  # pragma: no cover
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("httpx stub não deve ser usado nos golden tests")

    _make_module(
        "httpx",
        Response=_Response,
        AsyncClient=_AsyncClient,
        Client=_Client,
    )


# --------------------------------------------------------------------------- #
# 3. Stub dos módulos de serviço + url_validator.
# --------------------------------------------------------------------------- #
def _install_service_stubs() -> None:
    # Real __path__ on the parent packages so NON-stubbed submodules (e.g.
    # usage_service, memory_core, model_catalog, core.config) still resolve to
    # the real files under full-suite runs. The explicit stub submodules below
    # are registered in sys.modules and keep winning over the real ones. Without
    # a real __path__ the synthetic parents (empty __path__, no teardown) poison
    # `import app.services.*` for the whole pytest session.
    _make_package("app.services", _BACKEND_ROOT / "app" / "services")
    _make_package("app.core", _BACKEND_ROOT / "app" / "core")
    _make_package("app.core.security", _BACKEND_ROOT / "app" / "core" / "security")
    _make_package("app.core.database")

    def _missing_provider(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(
            "Provider real não disponível nos golden tests; "
            "injete um provider fake no construtor do Adapter."
        )

    _make_module("app.services.search_service", get_search_service=_missing_provider)
    _make_module("app.services.qdrant_service", get_qdrant_service=_missing_provider)
    _make_module("app.services.tavily_service", get_tavily_service=_missing_provider)
    _make_module(
        "app.services.filesystem_search_service",
        get_filesystem_search_service=_missing_provider,
    )
    _make_module("app.core.database", get_supabase_client=_missing_provider)

    # Mesmo gate dos stubs de langchain/httpx: com o módulo REAL importável
    # (é código do repo, stdlib-only), NÃO sombrear. Os goldens nunca CHAMAM o
    # validator (o stub levanta se usado); já as suítes irmãs coletadas depois
    # na suíte completa (tests/security, tests/services/test_remote_mcp_service,
    # test_ucp_auth_ssrf) dependem do comportamento real de
    # validate_external_url e quebravam com o stub envenenando sys.modules.
    if not _real_dep_available("app.core.security.url_validator"):
        class _ExternalUrlValidationError(Exception):
            pass

        def _validate_external_url(url: str) -> object:
            raise RuntimeError(
                "url_validator stub não deve ser usado nos golden tests"
            )

        def _revalidate_external_url(validated: object) -> None:
            raise RuntimeError(
                "url_validator stub não deve ser usado nos golden tests"
            )

        _make_module(
            "app.core.security.url_validator",
            ExternalUrlValidationError=_ExternalUrlValidationError,
            validate_external_url=_validate_external_url,
            revalidate_external_url=_revalidate_external_url,
        )


# --------------------------------------------------------------------------- #
# 4. Pacotes sintéticos app.agents / app.agents.tools (sem rodar __init__).
# --------------------------------------------------------------------------- #
def _install_agents_packages() -> None:
    import app  # noqa: F401  (pacote real e leve)

    agents_path = _BACKEND_ROOT / "app" / "agents"
    _make_package("app.agents", agents_path)
    _make_package("app.agents.tools", agents_path / "tools")


def _bootstrap() -> None:
    _ensure_backend_on_path()
    _install_langchain_stub()
    _install_httpx_stub()
    _install_service_stubs()
    _install_agents_packages()


_bootstrap()
