"""
UCP Tool Factory - Gera Adapters AgentTool DINAMICAMENTE a partir do manifest UCP.

IMPORTANTE: Esta versão NÃO usa definições hardcoded.
As tools são geradas a partir das capabilities declaradas no manifest da loja.

Arquitetura (Tool Runtime):
- DynamicUCPTool herda de AgentTool (NÃO de BaseTool). A compatibilidade com
  llm.bind_tools() é feita pelo LangChainToolShim do Registry, por composição.
- A execução usa o UCPService (mantido): execute() chama
  UCPService.execute_capability(agent_id=context.agent_id, ...). NÃO há mais
  discovery/transport internos no Adapter nem singleton global de tenant — a
  identidade (agent_id, company_id) vem SEMPRE do ToolExecutionContext.
- Retorna ToolResult canônico com truncamento semântico para respostas grandes
  (raw_for_log preserva o payload completo).

Referência: https://ucp.dev/specification/overview/
"""

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Type

from pydantic import BaseModel, Field, create_model

from app.schemas.ucp_manifest import UCPCapability, UCPManifest

from ..runtime import AgentTool, ToolExecutionContext, ToolResult

logger = logging.getLogger(__name__)

# Teto semântico do texto enviado ao LLM. Respostas UCP podem ser grandes
# (catálogos, checkouts); truncamos o content_for_llm preservando o payload
# completo em raw_for_log para conversation_logs / debug.
MAX_UCP_CONTENT_CHARS = 8000

# Provider que devolve o UCPService (injetável em testes).
UCPServiceProvider = Callable[[], Any]

# Tipos de erro do UCPService que indicam falha de conexão/gateway.
_UCP_GATEWAY_ERROR_TYPES = {
    "transport_error",
    "no_connection",
    "no_manifest",
    "unsupported_capability",
}


def _default_ucp_service_provider() -> Any:
    from app.services.ucp_service import get_ucp_service

    return get_ucp_service()


def _truncate_for_llm(content: str, metadata: Dict[str, Any]) -> str:
    """Aplica truncamento semântico ao texto enviado ao LLM.

    Marca metadata['truncated']=True quando o teto é excedido. O caller mantém
    o payload completo em raw_for_log.
    """
    if len(content) > MAX_UCP_CONTENT_CHARS:
        metadata["truncated"] = True
        return content[:MAX_UCP_CONTENT_CHARS]
    return content


# =========================================================
# Dynamic Input Schema Generation
# =========================================================

class GenericUCPInput(BaseModel):
    """
    Schema genérico para capabilities UCP.

    Usado quando não temos schema específico da capability.
    Aceita qualquer parâmetro como kwargs.
    """
    query: Optional[str] = Field(
        default=None,
        description="Termo de busca ou consulta"
    )
    item_id: Optional[str] = Field(
        default=None,
        description="ID do item (produto, pedido, etc.)"
    )
    variant_id: Optional[str] = Field(
        default=None,
        description="ID da variante do produto (Shopify variant GID)"
    )
    quantity: Optional[int] = Field(
        default=1,
        description="Quantidade do item"
    )
    ucp_session_id: Optional[str] = Field(
        default=None,
        description="ID da sessão UCP (para operações multi-step)"
    )
    cart_id: Optional[str] = Field(
        default=None,
        description="ID do carrinho Shopify (gid://shopify/Cart/...). Informe para ATUALIZAR um carrinho existente; omita para criar um novo."
    )
    add_items: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Itens para adicionar ao carrinho. Formato: [{'product_variant_id': 'gid://shopify/ProductVariant/...', 'quantity': 1}]"
    )
    buyer_email: Optional[str] = Field(
        default=None,
        description="Email do comprador"
    )
    extra_params: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Parâmetros adicionais específicos da capability"
    )


def create_input_schema_from_json_schema(
    schema: Dict[str, Any],
    capability_name: str
) -> Type[BaseModel]:
    """
    Gera um Pydantic model a partir de um JSON Schema.

    Isso permite que qualquer capability com schema seja usável.
    """
    if not schema or "properties" not in schema:
        return GenericUCPInput

    properties = schema.get("properties", {})
    required = schema.get("required", [])

    # Mapear tipos JSON Schema -> Python
    # NOTA: Gemini exige 'items' type em arrays. Usamos List[str] como
    # fallback seguro para arrays e Dict[str, str] para objects.
    type_mapping = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": List[str],
        "object": Dict[str, str]
    }

    # Construir campos do modelo
    fields = {}
    for name, prop in properties.items():
        prop_type = prop.get("type", "string")
        python_type = type_mapping.get(prop_type, str)
        description = prop.get("description", f"Parameter: {name}")
        default = prop.get("default")

        if name in required:
            # Campo obrigatório
            if default is not None:
                fields[name] = (python_type, Field(default=default, description=description))
            else:
                fields[name] = (python_type, Field(..., description=description))
        else:
            # Campo opcional
            optional_type = Optional[python_type]
            fields[name] = (optional_type, Field(default=default, description=description))

    if not fields:
        return GenericUCPInput

    # Gerar nome único para o modelo
    model_name = f"UCP{capability_name.replace('.', '_').title()}Input"

    try:
        return create_model(model_name, **fields)
    except Exception as e:
        logger.warning(f"[UCP Factory] Erro ao criar schema dinâmico: {e}")
        return GenericUCPInput


# =========================================================
# Dynamic UCP Tool (AgentTool Adapter)
# =========================================================

class DynamicUCPTool(AgentTool):
    """
    Adapter AgentTool que executa capabilities UCP via UCPService.

    Criado dinamicamente a partir do manifest da loja. Não depende de provider
    específico (Shopify, etc.) nem de singleton de tenant: a identidade vem do
    ToolExecutionContext e a execução é delegada ao UCPService (mantido).
    """

    # Schema vem do manifest da loja (terceiro): parâmetros coincidentes com
    # campos do ToolExecutionContext são da capability, não contexto injetado.
    # A identidade/tenant vem SEMPRE do context — isenta o guard de leak.
    allows_context_field_args: bool = True

    def __init__(
        self,
        *,
        name: str,
        description: str,
        ucp_capability: str,
        store_url: str,
        args_schema: Type[BaseModel] = GenericUCPInput,
        transport_type: str = "rest",
        ucp_service_provider: Optional[UCPServiceProvider] = None,
    ) -> None:
        self.name = name
        self.description = description
        self.args_schema = args_schema

        # Metadata UCP (config, não tenant).
        self._ucp_capability = ucp_capability
        self._store_url = store_url
        self._transport_type = transport_type

        # Provider do UCPService (infra singleton, injetável em testes).
        self._ucp_service_provider = (
            ucp_service_provider or _default_ucp_service_provider
        )

    @property
    def ucp_capability(self) -> str:
        return self._ucp_capability

    @property
    def store_url(self) -> str:
        return self._store_url

    def get_required_context(self) -> List[str]:
        return ["agent_id", "session_id", "company_id", "allowed_http_tools"]

    def allowed_in_subagent(self) -> bool:
        # Checkout renderiza UI no front (carrossel/checkout) e não deve ser
        # delegado a SubAgents — mesma exclusão da arquitetura legada.
        return not self._ucp_capability.endswith("checkout")

    async def execute(
        self, context: ToolExecutionContext, **kwargs: Any
    ) -> ToolResult:
        logger.info(
            "[UCP Tool] 🛒 Executando %s (%s) para %s | agent=%s",
            self.name,
            self._ucp_capability,
            self._store_url,
            context.agent_id,
        )

        metadata: Dict[str, Any] = {
            "tool_kind": "ucp",
            "ucp_capability": self._ucp_capability,
            "store_url": self._store_url,
        }

        # dev.ucp.shopping.checkout -> checkout
        parts = self._ucp_capability.split(".")
        capability_action = parts[-1] if parts else self._ucp_capability

        params = self._prepare_params(kwargs)

        service = self._ucp_service_provider()
        result = await service.execute_capability(
            agent_id=context.agent_id,
            capability=self._ucp_capability,
            params=params,
            store_url=self._store_url,
        )

        if not isinstance(result, dict):
            result = {"result": result}

        # Erro reportado pelo UCPService.
        if result.get("error"):
            error_type = result.get("type")
            error_kind = (
                "gateway" if error_type in _UCP_GATEWAY_ERROR_TYPES else "downstream"
            )
            content = json.dumps(result, ensure_ascii=False)
            return ToolResult(
                content_for_llm=_truncate_for_llm(content, metadata),
                raw_for_log=result,
                is_error=True,
                error_kind=error_kind,
                metadata=metadata,
            )

        # 🔥 Fluxo de carrinho: o update_cart/get_cart devolve o carrinho dentro
        # de `cart`, com `cart.id` (cart_id) e `cart.checkout_url`. Expomos esses
        # campos no topo do resultado para o agente/UI navegar até o checkout
        # sem ter que escarafunchar a estrutura aninhada.
        if self._ucp_capability.endswith("checkout"):
            cart = result.get("cart")
            if isinstance(cart, dict):
                checkout_url = cart.get("checkout_url")
                cart_id = cart.get("id")
                if checkout_url:
                    result["checkout_url"] = checkout_url
                if cart_id:
                    result["cart_id"] = cart_id

        # Sucesso: anexa metadata UCP para renderização no frontend.
        result["_ucp_metadata"] = {
            "type": self._determine_response_type(capability_action),
            "capability": self._ucp_capability,
            "store_url": self._store_url,
        }

        content = json.dumps(result, ensure_ascii=False, indent=2)
        return ToolResult(
            content_for_llm=_truncate_for_llm(content, metadata),
            raw_for_log=result,
            metadata=metadata,
        )

    def _prepare_params(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """
        Prepara parâmetros para a chamada.

        Para checkout (fluxo de CARRINHO da Shopify), converte
        variant_id/quantity para o input `add_items` do tool `update_cart`:
            add_items: [{"product_variant_id": "gid://...", "quantity": 1}]

        O schema do update_cart só exige product_variant_id + quantity (ambos
        por item). cart_id é opcional: se presente, atualiza o carrinho
        existente; se ausente, cria um novo. Não há mais idempotency_key/_meta.
        """
        params: Dict[str, Any] = {}

        for key, value in kwargs.items():
            if value is not None:
                # Expandir extra_params se presente
                if key == "extra_params" and isinstance(value, dict):
                    params.update(value)
                else:
                    params[key] = value

        # 🔥 Fluxo de carrinho: converter variant_id/quantity para add_items.
        # Formato Shopify update_cart: add_items: [{"product_variant_id": "gid://...", "quantity": 1}]
        if self._ucp_capability.endswith("checkout"):
            variant_id = params.pop("variant_id", None) or params.pop("item_id", None)
            quantity = params.pop("quantity", 1) or 1
            buyer_email = params.pop("buyer_email", None)

            # Construir add_items se variant_id fornecido e add_items não presente.
            if variant_id and "add_items" not in params:
                params["add_items"] = [{
                    "product_variant_id": variant_id,
                    "quantity": quantity,
                }]
                logger.info(f"[UCP Tool] Auto-converted variant_id to add_items: {variant_id}")

            # Buyer email -> buyer_identity (schema do update_cart).
            if buyer_email and "buyer_identity" not in params:
                params["buyer_identity"] = {"email": buyer_email}

        return params

    def _determine_response_type(self, action: str) -> str:
        """Determina tipo de resposta para renderização no frontend."""
        if action in ["checkout", "cart", "payment"]:
            return "ucp_checkout"
        elif action in ["catalog", "search", "products"]:
            return "ucp_product_list"
        elif action in ["product", "item", "detail"]:
            return "ucp_product_detail"
        elif action in ["order", "fulfillment", "tracking"]:
            return "ucp_order"
        else:
            return "ucp_generic"


# =========================================================
# UCP Tool Factory
# =========================================================

class UCPToolFactory:
    """
    Factory que cria Adapters AgentTool DINAMICAMENTE a partir do manifest UCP.

    Discovery LAZY: apenas materializa Adapters; nenhuma conexão é aberta aqui.
    """

    @staticmethod
    async def create_tools_from_manifest(
        store_url: str,
        manifest: UCPManifest,
        preferred_transport: Optional[str] = None,
        schema_cache: Optional[Dict[str, Dict]] = None,
        address_country: Optional[str] = None,
    ) -> List[AgentTool]:
        """
        Cria tools a partir das capabilities do manifest.

        Args:
            address_country: País (ISO-3166 alpha-2) do tenant (agente/loja),
                repassado às Storefront tools. ``None`` => fallback 'BR' com aviso
                no client (BAIXO-006).

        Returns:
            Lista de Adapters AgentTool (DynamicUCPTool + Storefront) prontos.
        """
        tools: List[AgentTool] = []
        transport = preferred_transport or manifest.get_preferred_transport() or "rest"

        capabilities = manifest.get_capabilities()

        if not capabilities:
            logger.warning(f"[UCP Factory] Nenhuma capability no manifest de {store_url}")
            return []

        logger.info(f"[UCP Factory] Criando tools para {len(capabilities)} capabilities")

        for capability in capabilities:
            try:
                tool = await UCPToolFactory._create_tool_from_capability(
                    capability=capability,
                    store_url=store_url,
                    transport_type=transport,
                    schema_cache=schema_cache
                )
                if tool:
                    tools.append(tool)
            except Exception as e:  # noqa: BLE001 - melhor esforço por capability
                logger.error(f"[UCP Factory] Erro ao criar tool para {capability.name}: {e}")

        # =========================================================
        # STOREFRONT MCP: Adicionar tools de busca de produtos
        # =========================================================
        try:
            from app.agents.tools.storefront_catalog_tool import create_storefront_tools

            # Extrair nome amigável da loja
            store_name = store_url.replace("https://", "").split(".")[0]

            storefront_tools = create_storefront_tools(
                store_url=store_url,
                store_name=store_name,
                address_country=address_country,
            )
            tools.extend(storefront_tools)

            logger.info(f"[UCP Factory] ✅ +{len(storefront_tools)} Storefront tools adicionadas")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[UCP Factory] Erro ao criar Storefront tools: {e}")

        logger.info(f"[UCP Factory] ✅ {len(tools)} tools criadas para {store_url}")
        return tools

    @staticmethod
    async def _create_tool_from_capability(
        capability: UCPCapability,
        store_url: str,
        transport_type: str,
        schema_cache: Optional[Dict[str, Dict]] = None
    ) -> Optional[DynamicUCPTool]:
        """Cria um DynamicUCPTool a partir de uma capability."""

        # Obter schema se disponível
        input_schema: Type[BaseModel] = GenericUCPInput

        if capability.schema_url:
            # Tentar buscar schema
            if schema_cache and capability.schema_url in schema_cache:
                schema = schema_cache[capability.schema_url]
            else:
                from app.services.ucp_discovery import get_ucp_discovery_service
                discovery = get_ucp_discovery_service()
                schema = await discovery.get_capability_schema(capability)

                if schema and schema_cache is not None:
                    schema_cache[capability.schema_url] = schema

            if schema:
                input_schema = create_input_schema_from_json_schema(
                    schema=schema,
                    capability_name=capability.name
                )

        # Construir descrição
        description = UCPToolFactory._build_description(capability, store_url)

        return DynamicUCPTool(
            name=capability.tool_name,
            description=description,
            args_schema=input_schema,
            ucp_capability=capability.name,
            store_url=store_url,
            transport_type=transport_type
        )

    @staticmethod
    def _build_description(capability: UCPCapability, store_url: str) -> str:
        """Constrói descrição da tool para o LLM."""
        # Mapear capabilities conhecidas para descrições melhores
        descriptions = {
            "checkout": f"Cria sessão de checkout para comprar itens da loja {store_url}",
            "catalog": f"Busca produtos no catálogo da loja {store_url}",
            "fulfillment": f"Consulta status de entrega e rastreamento em {store_url}",
            "order": f"Consulta informações de pedidos em {store_url}",
            "discount": f"Aplica cupons de desconto em {store_url}",
            "identity": f"Vincula identidade do usuário com {store_url}",
        }

        # Extrair ação da capability
        action = capability.short_name.split("_")[-1]

        if action in descriptions:
            return descriptions[action]

        # Descrição genérica
        return f"Executa capability UCP '{capability.name}' na loja {store_url}"

    @staticmethod
    async def create_tools_for_agent(agent_id: str) -> List[AgentTool]:
        """
        Cria todas as tools UCP (incluindo Storefront) para um agente.
        Carrega conexões ativas do banco de dados.
        """
        try:
            from app.services.ucp_discovery import get_ucp_discovery_service

            discovery = get_ucp_discovery_service()
            discoveries = await discovery.load_from_database(agent_id)

            tools: List[AgentTool] = []
            for result in discoveries:
                if result.manifest:
                    # Cria tools para esta conexão (UCP + Storefront)
                    conn_tools = await UCPToolFactory.create_tools_from_manifest(
                        store_url=result.store_url,
                        manifest=result.manifest,
                        preferred_transport=result.preferred_transport,
                        address_country=result.address_country,
                    )
                    tools.extend(conn_tools)

            return tools
        except Exception as e:  # noqa: BLE001
            logger.error(f"[UCP Factory] Erro ao criar tools para agente {agent_id}: {e}")
            return []


# =========================================================
# Helpers para Prompt Editor
# =========================================================

def get_ucp_tools_for_prompt(manifest: UCPManifest, store_url: str) -> List[Dict[str, Any]]:
    """
    Formata tools UCP para o dropdown de variáveis do frontend.

    Inclui:
    - Capabilities do manifest (checkout, fulfillment, etc.)
    - Storefront MCP tools (busca de produtos, políticas)
    """
    tools_info = []
    store_name = store_url.replace("https://", "").split(".")[0]

    # 1. Capabilities do manifest UCP
    for capability in manifest.get_capabilities():
        tools_info.append({
            "name": capability.tool_name,
            "description": f"UCP: {capability.name}",
            "type": "ucp",
            "capability": capability.name,
            "version": capability.version,
            "store": store_url,
            "is_extension": capability.is_extension
        })

    # 2. Storefront MCP Tools (busca de produtos - sempre disponível)
    tools_info.append({
        "name": "store_product_search",
        "description": f"Busca produtos na loja {store_name}",
        "type": "storefront",
        "capability": "storefront.catalog",
        "store": store_url,
        "is_extension": False
    })

    tools_info.append({
        "name": "store_policy_search",
        "description": f"Perguntas sobre políticas da loja {store_name}",
        "type": "storefront",
        "capability": "storefront.policies",
        "store": store_url,
        "is_extension": False
    })

    return tools_info


async def get_all_ucp_tools_for_agent(agent_id: str) -> List[Dict[str, Any]]:
    """
    Retorna todas as tools UCP disponíveis para um agente.

    Carrega do banco de dados e formata para o prompt editor.
    """
    from app.services.ucp_discovery import get_ucp_discovery_service

    discovery = get_ucp_discovery_service()
    discoveries = await discovery.load_from_database(agent_id)

    all_tools = []
    for result in discoveries:
        if result.manifest:
            tools = get_ucp_tools_for_prompt(result.manifest, result.store_url)
            all_tools.extend(tools)

    return all_tools
