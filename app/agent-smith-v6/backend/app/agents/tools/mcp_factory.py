"""
MCP Tool Factory - Cria Adapters AgentTool a partir das configurações do banco.

Arquitetura (Tool Runtime):
- MCPFactoryTool herda de AgentTool (NÃO de BaseTool). A compatibilidade com
  llm.bind_tools() é feita pelo LangChainToolShim do Registry, por composição.
- Discovery é LAZY: a factory apenas materializa Adapters (name/description/
  args_schema + metadata do servidor/tool). NENHUMA conexão MCP é aberta no
  discovery — o subprocesso só é iniciado em execute(), via MCPGatewayService.
- Identidade multi-tenant (agent_id) vem SEMPRE do ToolExecutionContext em
  runtime — nunca de atributos de instância. Garante isolamento correto entre
  execuções concorrentes.
- execute() é cancellation-safe: a chamada ao gateway é protegida por
  asyncio.shield, de modo que um cancelamento/timeout externo NÃO abandona o
  subprocesso no meio da comunicação (o gateway tem timeout interno próprio que
  encerra o processo), evitando subprocessos órfãos.
- Retorna ToolResult canônico. content_for_llm preserva exatamente o texto que a
  versão legada (DynamicMCPTool) produzia (paridade de golden test).
"""

import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Optional, Type

from pydantic import BaseModel, Field, create_model

from ..runtime import AgentTool, ToolExecutionContext, ToolResult

logger = logging.getLogger(__name__)

# Provider que devolve o MCPGatewayService (injetável em testes).
GatewayProvider = Callable[[], Any]

# Tag XML usada para delimitar output de MCP REMOTO no prompt (SPEC design §7:
# conteúdo de terceiros é não confiável). Também serve de label no
# enforce_prompt_safety do Runtime.
REMOTE_MCP_WRAP_XML_TAG = "mcp_remote_result"


def _default_gateway_provider() -> Any:
    from ...services.mcp_gateway_service import get_mcp_gateway

    return get_mcp_gateway()


class MCPFactoryTool(AgentTool):
    """
    Adapter que executa uma tool específica de um MCP Server interno.

    Materializada em runtime pela MCPToolFactory a partir das configs de
    agent_mcp_tools. O discovery NÃO abre conexão; o subprocesso MCP só é
    iniciado em execute(), via MCPGatewayService (mantido).
    """

    # supports_cancellation=False: o subprocesso MCP é encerrado pelo timeout
    # INTERNO do gateway (asyncio.wait_for na communicate). O Runtime não deve
    # cancelar o execute no meio — combinado com o asyncio.shield interno, isso
    # garante que nenhum subprocesso fique órfão em caso de timeout.
    supports_cancellation: bool = False

    # Schema vem do servidor MCP (terceiro): um parâmetro como `user_id`
    # (ex.: notion-get-users) é legítimo e é repassado opaco ao servidor; a
    # identidade/tenant vem SEMPRE de context.agent_id, nunca dos kwargs. Logo,
    # colisão de nome com ToolExecutionContext não é vazamento — isenta o guard.
    allows_context_field_args: bool = True

    def __init__(
        self,
        *,
        variable_name: str,
        tool_name: str,
        mcp_server_name: str,
        description: str,
        args_schema: Type[BaseModel],
        gateway_provider: Optional[GatewayProvider] = None,
    ) -> None:
        # name/description/args_schema são definidos por instância (tool dinâmica).
        self.name = variable_name
        self.description = description
        self.args_schema = args_schema

        # Metadata MCP (config, não tenant).
        self._mcp_server_name = mcp_server_name
        self._mcp_tool_name = tool_name

        # Provider do gateway (infra singleton, injetável em testes).
        self._gateway_provider = gateway_provider or _default_gateway_provider

    @property
    def mcp_server_name(self) -> str:
        return self._mcp_server_name

    @property
    def mcp_tool_name(self) -> str:
        return self._mcp_tool_name

    def get_required_context(self) -> List[str]:
        return ["agent_id", "session_id", "is_subagent"]

    async def execute(
        self, context: ToolExecutionContext, **kwargs: Any
    ) -> ToolResult:
        logger.info(
            "[MCP Tool] 🔗 %s.%s | agent=%s",
            self._mcp_server_name,
            self._mcp_tool_name,
            context.agent_id,
        )

        gateway = self._gateway_provider()

        metadata: Dict[str, Any] = {
            "tool_kind": "mcp",
            "mcp_server": self._mcp_server_name,
            "mcp_tool": self._mcp_tool_name,
        }

        # Remove parâmetros None antes de enviar ao MCP. O schema dinâmico torna
        # todo campo opcional `Optional[T]` com default None, então o LLM (ou o
        # Pydantic) preenche os omitidos com None. Encaminhar esses None ao
        # servidor SOBRESCREVE os defaults server-side (ex.: calendar_id="primary"
        # virava None → URL /calendars/None/events → 404). Omitir = semântica
        # JSON-RPC correta: o servidor aplica seu próprio default.
        params = {k: v for k, v in kwargs.items() if v is not None}

        # Cancellation-safe: shield garante que, mesmo sob cancel/timeout externo,
        # a coroutine do gateway prossiga até concluir o ciclo do subprocesso
        # (que tem timeout interno próprio), evitando subprocessos órfãos.
        call = gateway.call_mcp_tool(
            agent_id=context.agent_id,
            mcp_server_name=self._mcp_server_name,
            tool_name=self._mcp_tool_name,
            params=params,
        )
        result = await asyncio.shield(call)

        if result.get("success"):
            data = result.get("result", {})
            if isinstance(data, dict):
                content = json.dumps(data, ensure_ascii=False, indent=2)
            else:
                content = str(data)
            # SPEC design §7 — prompt injection via servidor de terceiro:
            # payloads de MCP REMOTO chegam do gateway marcados com
            # untrusted_content=True (RemoteMCPService). Como é a factory que
            # materializa o ToolResult, a fiação acontece aqui: liga
            # requires_prompt_safety (Runtime aplica enforce_prompt_safety em
            # registry._execute) e delimita o conteúdo com wrap_xml_tag —
            # decisão por FLAG do payload, nunca por nome de tool/servidor.
            # Servers internos (sem a flag) mantêm o comportamento legado.
            untrusted = bool(result.get("untrusted_content"))
            return ToolResult(
                content_for_llm=content,
                raw_for_log=result,
                requires_prompt_safety=untrusted,
                wrap_xml_tag=REMOTE_MCP_WRAP_XML_TAG if untrusted else None,
                metadata={
                    **metadata,
                    "success": True,
                    "untrusted_content": untrusted,
                },
            )

        # MCP server down / erro de gateway => error_kind='gateway'.
        error = result.get("error", "Erro desconhecido")
        return ToolResult(
            content_for_llm=f"❌ Erro: {error}",
            raw_for_log=result,
            is_error=True,
            error_kind="gateway",
            metadata={**metadata, "success": False},
        )


class MCPToolFactory:
    """
    Factory que materializa Adapters MCPFactoryTool a partir das configs do banco.

    Discovery LAZY: não abre conexão nem inicia subprocesso — apenas constrói os
    Adapters com seus schemas. A conexão é diferida para execute().
    """

    @staticmethod
    def create_tools_for_agent(
        agent_id: str,
        mcp_tools_config: List[Dict],
        gateway_provider: Optional[GatewayProvider] = None,
    ) -> List[MCPFactoryTool]:
        """
        Cria lista de MCPFactoryTool a partir das configs de agent_mcp_tools.

        Args:
            agent_id: ID do agente (mantido na assinatura por compatibilidade;
                a identidade efetiva vem do ToolExecutionContext em runtime).
            mcp_tools_config: Lista de configs de agent_mcp_tools.
            gateway_provider: Provider do MCPGatewayService (injetável em testes).

        Returns:
            Lista de Adapters prontos para o Registry/bind.
        """
        tools: List[MCPFactoryTool] = []

        for config in mcp_tools_config:
            try:
                tool = MCPToolFactory._create_single_tool(config, gateway_provider)
                if tool:
                    tools.append(tool)
            except Exception as e:  # noqa: BLE001 - melhor esforço por config
                logger.error(f"[MCP Factory] Erro ao criar tool: {e}")

        logger.info(
            f"[MCP Factory] ✅ Criadas {len(tools)} tools MCP para agente {agent_id}"
        )
        return tools

    @staticmethod
    def _create_single_tool(
        config: Dict,
        gateway_provider: Optional[GatewayProvider] = None,
    ) -> Optional[MCPFactoryTool]:
        """Cria um único MCPFactoryTool a partir da config."""

        variable_name = config.get("variable_name", "")
        tool_name = config.get("tool_name", "")
        server_name = config.get("mcp_server_name", "")
        description = config.get("description", f"Executa {tool_name} via MCP")
        input_schema = config.get("input_schema", {})

        if not variable_name or not tool_name or not server_name:
            logger.warning(f"[MCP Factory] Config incompleta: {config}")
            return None

        # Criar Pydantic model dinamicamente a partir do input_schema
        InputModel = MCPToolFactory._create_input_model(variable_name, input_schema)

        return MCPFactoryTool(
            variable_name=variable_name,
            tool_name=tool_name,
            mcp_server_name=server_name,
            description=description,
            args_schema=InputModel,
            gateway_provider=gateway_provider,
        )

    @staticmethod
    def _create_input_model(tool_name: str, schema: Dict) -> Type[BaseModel]:
        """
        Cria um Pydantic model a partir do JSON Schema do MCP.
        """
        if not schema or not isinstance(schema, dict):
            # Schema vazio: tool sem parâmetros
            return create_model(f"{tool_name}_Input")

        properties = schema.get("properties", {})
        required = schema.get("required", [])

        fields = {}
        for prop_name, prop_schema in properties.items():
            # Mapear tipo JSON Schema -> Python (passa schema completo para arrays)
            python_type = MCPToolFactory._json_type_to_python(prop_schema)

            description = prop_schema.get("description", "")
            default = ... if prop_name in required else None

            fields[prop_name] = (
                python_type if prop_name in required else Optional[python_type],
                Field(default=default, description=description)
            )

        if not fields:
            return create_model(f"{tool_name}_Input")

        return create_model(f"{tool_name}_Input", **fields)

    @staticmethod
    def _json_type_to_python(prop_schema):
        """Mapeia tipo JSON Schema para tipo Python.

        Para arrays, resolve o tipo interno via 'items' para que
        o Pydantic gere JSON Schema com 'items' (exigido pelo Gemini).
        """
        # Aceita string legada ou dict completo
        if isinstance(prop_schema, str):
            json_type = prop_schema
            prop_schema = {}
        else:
            json_type = prop_schema.get("type", "string")

        base_mapping = {
            "string": str,
            "integer": int,
            "number": float,
            "boolean": bool,
            "object": dict,
        }

        if json_type == "array":
            items_schema = prop_schema.get("items", {})
            items_type = items_schema.get("type", "string") if isinstance(items_schema, dict) else "string"
            inner = base_mapping.get(items_type, str)
            return List[inner]

        return base_mapping.get(json_type, str)


# Alias de compatibilidade retroativa: imports legados de DynamicMCPTool continuam
# funcionando, agora apontando para o Adapter AgentTool.
DynamicMCPTool = MCPFactoryTool


def get_mcp_tools_for_prompt(agent_mcp_tools: List[Dict]) -> List[Dict]:
    """
    Formata MCP tools para o dropdown de variáveis do frontend.
    Retorna no mesmo formato das HTTP tools.
    """
    return [
        {
            "name": tool["variable_name"],
            "description": tool.get("description", ""),
            "type": "mcp",
            "mcp_server": tool.get("mcp_server_name", ""),
            "parameters": _extract_parameters(tool.get("input_schema", {}))
        }
        for tool in agent_mcp_tools
    ]


def _extract_parameters(schema: Dict) -> List[Dict]:
    """Extrai parâmetros do JSON Schema para exibição."""
    if not schema:
        return []

    properties = schema.get("properties", {})
    required = schema.get("required", [])

    params = []
    for name, prop in properties.items():
        params.append({
            "name": name,
            "type": prop.get("type", "string"),
            "description": prop.get("description", ""),
            "required": name in required
        })

    return params
