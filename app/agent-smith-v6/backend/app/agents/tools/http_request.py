"""
Ferramenta de Requisição HTTP Dinâmica — Executa chamadas HTTP configuradas no banco de dados.

Isso permite que os agentes chamem APIs externas sem exigir novo código Python.
As ferramentas são configuradas na tabela agent_http_tools.

Arquitetura (Tool Runtime):
- HttpToolRouter herda de AgentTool (NÃO de BaseTool).
- A autorização usa context.allowed_http_tools (subset de HTTP tools nomeadas no
  prompt do agente), substituindo a injeção por nome que existia em
  nodes.py (tool_args["allowed_tools"]).
- Retorna ToolResult: content_for_llm é a resposta truncada (teto semântico),
  raw_for_log carrega a resposta completa para conversation_logs / debug.

HttpRequestTool/create_dynamic_tool permanecem como helpers internos para
materializar e executar uma chamada HTTP específica configurada no banco.
"""

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, Type

import httpx
from app.core.security.url_validator import (
    ExternalUrlValidationError,
    revalidate_external_url,
    validate_external_url,
)
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, create_model

from ..runtime import AgentTool, ToolExecutionContext, ToolResult

logger = logging.getLogger(__name__)

HTTP_TOOL_TIMEOUT_SECONDS = 60.0
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
# Teto semântico do texto enviado ao LLM (truncamento semântico ~5 KB).
MAX_HTTP_CONTENT_CHARS = 5000


class ResponseTooLargeError(Exception):
    """Raised when an HTTP tool response exceeds the configured size limit."""


def _decode_response_body(body: bytes, encoding: Optional[str]) -> str:
    return body.decode(encoding or "utf-8", errors="replace")


def _read_limited_response(response: httpx.Response) -> str:
    chunks: list[bytes] = []
    total_bytes = 0

    for chunk in response.iter_bytes():
        total_bytes += len(chunk)
        if total_bytes > MAX_RESPONSE_BYTES:
            response.close()
            raise ResponseTooLargeError("HTTP tool response exceeded 10 MB")
        chunks.append(chunk)

    return _decode_response_body(b"".join(chunks), response.encoding)


async def _read_limited_response_async(response: httpx.Response) -> str:
    chunks: list[bytes] = []
    total_bytes = 0

    async for chunk in response.aiter_bytes():
        total_bytes += len(chunk)
        if total_bytes > MAX_RESPONSE_BYTES:
            await response.aclose()
            raise ResponseTooLargeError("HTTP tool response exceeded 10 MB")
        chunks.append(chunk)

    return _decode_response_body(b"".join(chunks), response.encoding)


class HttpRequestTool(BaseTool):
    """
    Tool genérica que executa requisições HTTP configuradas dinamicamente.
    Suporta execução Síncrona e Assíncrona.

    Helper interno do HttpToolRouter — não é exposto diretamente ao LLM.
    """

    name: str
    description: str
    args_schema: Type[BaseModel]
    target_url: str
    method: str
    headers: Dict[str, str]
    body_template: Optional[str] = None  # Template JSON com placeholders {{param}}

    def _prepare_request(self, kwargs):
        """Helper para preparar URL, Params e Body (com suporte a templates)"""
        logger.info(
            f"[HttpTool] 🚀 {self.name} ({self.method} {self.target_url}) | Params: {kwargs}"
        )

        # 1. Substituir variáveis de Path na URL (formato {param})
        #    Match case-INSENSITIVE: o placeholder {CEP} na URL casa com o kwarg
        #    'cep' (e vice-versa). Cada {placeholder} da URL é resolvido buscando
        #    um kwarg cujo nome bata ignorando case; o kwarg consumido é marcado
        #    como path_param para não vazar para a query/body.
        #    NOTA: isso NÃO conserta o clima — a Open-Meteo exige o valor em
        #    minúsculo do lado do servidor, o que é independente do casamento
        #    do nome do placeholder feito aqui.
        final_url = self.target_url
        path_params = {}
        # Índice case-insensitive dos kwargs: lower(name) -> name original.
        # Em colisão de case, preserva o primeiro (comportamento determinístico).
        kwargs_ci = {}
        for k in kwargs:
            kwargs_ci.setdefault(k.lower(), k)
        for placeholder in re.findall(r"\{(\w+)\}", final_url):
            if placeholder in path_params:
                continue  # placeholder repetido já resolvido
            # Match exato primeiro (preserva 100% o comportamento quando o case bate),
            # depois fallback case-insensitive.
            if placeholder in kwargs:
                matched_key = placeholder
            else:
                matched_key = kwargs_ci.get(placeholder.lower())
            if matched_key is None:
                continue  # nenhum kwarg corresponde a este placeholder
            value = kwargs[matched_key]
            final_url = final_url.replace(f"{{{placeholder}}}", str(value))
            path_params[matched_key] = value

        # 2. Parâmetros restantes (não usados na URL)
        remaining = {k: v for k, v in kwargs.items() if k not in path_params}

        # 3. Para GET, usar query params
        if self.method == "GET":
            return final_url, remaining, None

        # 4. Para POST/PUT/PATCH, verificar body_template
        json_body = None

        if self.body_template:
            try:
                body_str = self.body_template

                def replace_placeholder(match):
                    param_name = match.group(1)
                    value = remaining.get(param_name, "")
                    if isinstance(value, (int, float, bool)):
                        return (
                            str(value).lower()
                            if isinstance(value, bool)
                            else str(value)
                        )
                    return str(value)

                body_str = re.sub(r"\{\{(\w+)\}\}", replace_placeholder, body_str)
                json_body = json.loads(body_str)
                logger.info(f"[HttpTool] 📋 Body template processado: {json_body}")

            except json.JSONDecodeError as e:
                logger.warning(
                    f"[HttpTool] ⚠️ Erro ao processar body_template: {e}. Usando parâmetros diretamente."
                )
                json_body = remaining
        else:
            json_body = remaining if remaining else None

        return final_url, {}, json_body

    async def request_full(self, **kwargs) -> Tuple[int, str]:
        """Executa a chamada HTTP assíncrona e devolve (status_code, texto_completo).

        Sem truncamento — o caller decide como truncar para o LLM. Pode levantar
        ExternalUrlValidationError e ResponseTooLargeError.
        """
        url, params, json_body = self._prepare_request(kwargs)
        validated_url = validate_external_url(url)

        async with httpx.AsyncClient(
            timeout=HTTP_TOOL_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            revalidate_external_url(validated_url)
            async with client.stream(
                method=self.method,
                url=validated_url.normalized_url,
                headers=self.headers,
                params=params,
                json=json_body,
            ) as response:
                response_text = await _read_limited_response_async(response)
                return response.status_code, response_text

    def _run(self, **kwargs) -> Any:
        """Execução Síncrona (compatibilidade legada)."""
        try:
            url, params, json_body = self._prepare_request(kwargs)
            validated_url = validate_external_url(url)

            with httpx.Client(
                timeout=HTTP_TOOL_TIMEOUT_SECONDS, follow_redirects=False
            ) as client:
                revalidate_external_url(validated_url)
                with client.stream(
                    method=self.method,
                    url=validated_url.normalized_url,
                    headers=self.headers,
                    params=params,
                    json=json_body,
                ) as response:
                    response_text = _read_limited_response(response)

                    if response.status_code >= 400:
                        return f"Erro API ({response.status_code}): {response_text[:500]}"

                    return response_text[:MAX_HTTP_CONTENT_CHARS]

        except ExternalUrlValidationError:
            logger.warning("[HttpTool] URL bloqueada pelo validador SSRF")
            return "URL da ferramenta bloqueada pela política de segurança."
        except ResponseTooLargeError:
            logger.warning("[HttpTool] Resposta excedeu limite de 10 MB")
            return "Resposta da API excedeu o limite permitido."
        except Exception as e:
            logger.error(f"[HttpTool] Erro Sync: {e}", exc_info=True)
            return "Erro técnico interno na execução da ferramenta."

    async def _arun(self, **kwargs) -> Any:
        """Execução Assíncrona (compatibilidade legada)."""
        try:
            status_code, response_text = await self.request_full(**kwargs)
            if status_code >= 400:
                return f"Erro API ({status_code}): {response_text[:500]}"
            return response_text[:MAX_HTTP_CONTENT_CHARS]
        except ExternalUrlValidationError:
            logger.warning("[HttpTool] URL bloqueada pelo validador SSRF")
            return "URL da ferramenta bloqueada pela política de segurança."
        except ResponseTooLargeError:
            logger.warning("[HttpTool] Resposta excedeu limite de 10 MB")
            return "Resposta da API excedeu o limite permitido."
        except Exception as e:
            logger.error(f"[HttpTool] Erro Async: {e}", exc_info=True)
            return "Erro técnico interno na execução da ferramenta."


def create_dynamic_tool(tool_config: Dict) -> HttpRequestTool:
    """Factory que cria a Tool a partir do JSON do banco."""
    fields = {}
    for param in tool_config.get("parameters", []):
        param_type = int if param.get("type") == "integer" else str
        fields[param["name"]] = (
            param_type,
            Field(description=param.get("description", "")),
        )

    schema_name = f"{tool_config['name']}_Input"
    InputModel = create_model(schema_name, **fields)

    return HttpRequestTool(
        name=tool_config["name"],
        description=tool_config["description"],
        args_schema=InputModel,
        target_url=tool_config["url"],
        method=tool_config.get("method", "GET"),
        headers=tool_config.get("headers") or {},
        body_template=tool_config.get("body_template"),
    )


# === ROUTER TOOL: Carrega tools do banco a cada execução ===


class HttpToolRouterInput(BaseModel):
    """Input para o HttpToolRouter - apenas o nome da tool e parâmetros em JSON."""

    tool_name: str = Field(description="Nome da ferramenta HTTP a executar")
    params: str = Field(default="{}", description="Parâmetros em formato JSON")


class HttpToolRouter(AgentTool):
    """
    Tool Router que carrega dinamicamente as HTTP tools do banco de dados.
    Garante que cada agente só acesse suas próprias tools.

    IMPORTANTE: Só executa tools que foram MENCIONADAS no prompt do agente. A
    lista autorizada vem de context.allowed_http_tools (não mais injetada por
    nome em nodes.py). O agent_id também vem do contexto (isolamento multi-tenant).
    """

    name = "http_api"
    description = (
        "Executa chamadas HTTP para APIs externas. Use APENAS as ferramentas que "
        "foram descritas no prompt do sistema. Passe tool_name com o nome da "
        "ferramenta e params com os parâmetros em JSON."
    )
    args_schema: Type[BaseModel] = HttpToolRouterInput

    def __init__(self, supabase_client: Optional[Any] = None) -> None:
        # Cliente Supabase injetável (infra, não tenant). Resolução lazy do
        # singleton de conexão quando não fornecido.
        self._supabase_client = supabase_client

    def get_required_context(self) -> List[str]:
        return ["agent_id", "session_id", "allowed_http_tools"]

    def get_prompt_metadata(
        self, context: ToolExecutionContext
    ) -> Optional[str]:
        # Emite a BULA (manual) de cada HTTP tool ativa: nome, método, descrição,
        # parâmetros e como invocar via 'http_api'. As specs vêm projetadas em
        # context.http_tool_specs (apenas {name, method, description, parameters};
        # sem url/headers/body_template). Retorna None quando não há tools — o
        # Registry mantém o fallback da linha "HTTP tools autorizadas: ...".
        specs = context.http_tool_specs or []
        if not specs:
            return None

        from app.core.prompts import render_http_tool_bula

        bulas = [render_http_tool_bula(spec) for spec in specs]
        return "\n\n".join(b for b in bulas if b)

    def _get_client(self) -> Any:
        if self._supabase_client is not None:
            return self._supabase_client
        from app.core.database import get_supabase_client

        return get_supabase_client().client

    async def execute(
        self, context: ToolExecutionContext, **kwargs: Any
    ) -> ToolResult:
        tool_name: str = kwargs.get("tool_name", "")
        params: str = kwargs.get("params", "{}")
        allowed_tools: List[str] = list(context.allowed_http_tools or [])
        agent_id = context.agent_id

        logger.info(
            "[HttpToolRouter] 🔍 Agent %s requesting tool: %s", agent_id, tool_name
        )

        # === VERIFICAÇÃO DE AUTORIZAÇÃO (via context.allowed_http_tools) ===
        if not allowed_tools:
            return ToolResult(
                content_for_llm=(
                    f"❌ Ferramenta '{tool_name}' não autorizada.\n\n"
                    f"Nenhuma ferramenta HTTP foi configurada no prompt deste agente.\n"
                    f"Para usar ferramentas HTTP, o administrador deve incluir "
                    f"{{nome_da_ferramenta}} no prompt do agente."
                ),
                is_error=True,
                error_kind="auth",
                metadata={"tool_kind": "http"},
            )

        if tool_name not in allowed_tools:
            return ToolResult(
                content_for_llm=(
                    f"❌ Ferramenta '{tool_name}' não autorizada.\n\n"
                    f"Esta ferramenta não foi mencionada no prompt do agente.\n"
                    f"Ferramentas disponíveis neste contexto: "
                    f"{', '.join(allowed_tools)}"
                ),
                is_error=True,
                error_kind="auth",
                metadata={"tool_kind": "http"},
            )

        logger.info(
            "[HttpToolRouter] ✅ Tool '%s' autorizada (mencionada no prompt)",
            tool_name,
        )

        # === BUSCA DA CONFIG (Supabase é síncrono; offload) ===
        loop = asyncio.get_running_loop()
        tool_config = await loop.run_in_executor(
            None, lambda: self._fetch_tool_config(agent_id, tool_name)
        )

        if tool_config is None:
            available = await loop.run_in_executor(
                None, lambda: self._available_tools_description(agent_id)
            )
            return ToolResult(
                content_for_llm=(
                    f"Ferramenta '{tool_name}' não encontrada para este agente.\n\n"
                    f"Ferramentas disponíveis:\n{available}"
                ),
                is_error=True,
                error_kind="downstream",
                metadata={"tool_kind": "http"},
            )

        logger.info(
            "[HttpToolRouter] ✅ Found tool config: %s", tool_config["name"]
        )

        # Parse dos parâmetros.
        try:
            call_kwargs = json.loads(params) if params else {}
        except json.JSONDecodeError:
            call_kwargs = {}

        dynamic_tool = create_dynamic_tool(tool_config)

        # === EXECUÇÃO HTTP (assíncrona) ===
        try:
            status_code, full_text = await dynamic_tool.request_full(**call_kwargs)
        except ExternalUrlValidationError:
            logger.warning("[HttpToolRouter] URL bloqueada pelo validador SSRF")
            return ToolResult(
                content_for_llm="URL da ferramenta bloqueada pela política de segurança.",
                is_error=True,
                error_kind="auth",
                metadata={"tool_kind": "http"},
            )
        except ResponseTooLargeError:
            logger.warning("[HttpToolRouter] Resposta excedeu limite de 10 MB")
            return ToolResult(
                content_for_llm="Resposta da API excedeu o limite permitido.",
                is_error=True,
                error_kind="downstream",
                metadata={"tool_kind": "http"},
            )

        if status_code >= 400:
            return ToolResult(
                content_for_llm=f"Erro API ({status_code}): {full_text[:500]}",
                raw_for_log=full_text,
                is_error=True,
                error_kind="downstream",
                metadata={"tool_kind": "http", "status_code": status_code},
            )

        # Truncamento semântico do texto enviado ao LLM.
        content_for_llm = full_text[:MAX_HTTP_CONTENT_CHARS]
        metadata: Dict[str, Any] = {
            "tool_kind": "http",
            "status_code": status_code,
        }
        if len(full_text) > MAX_HTTP_CONTENT_CHARS:
            metadata["truncated"] = True

        return ToolResult(
            content_for_llm=content_for_llm,
            raw_for_log=full_text,
            metadata=metadata,
        )

    def _fetch_tool_config(
        self, agent_id: str, tool_name: str
    ) -> Optional[Dict[str, Any]]:
        """Busca a config de uma HTTP tool específica do agente (síncrono)."""
        response = (
            self._get_client()
            .table("agent_http_tools")
            .select("*")
            .eq("agent_id", agent_id)
            .eq("name", tool_name)
            .eq("is_active", True)
            .execute()
        )
        if not response.data:
            return None
        return response.data[0]

    def _available_tools_description(self, agent_id: str) -> str:
        """Descrição das tools HTTP disponíveis para o agente (síncrono)."""
        try:
            response = (
                self._get_client()
                .table("agent_http_tools")
                .select("name, description, method")
                .eq("agent_id", agent_id)
                .eq("is_active", True)
                .execute()
            )
            if response.data:
                tools_desc = [
                    f"- {t['name']}: {t['description']} ({t['method']})"
                    for t in response.data
                ]
                return "\n".join(tools_desc)
        except Exception as e:  # noqa: BLE001 - melhor esforço para mensagem ao LLM
            logger.error(f"[HttpToolRouter] Error fetching tools: {e}")

        return "Nenhuma ferramenta HTTP configurada."
