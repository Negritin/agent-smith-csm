"""
UCP Manifest Schema - Pydantic models para o manifesto UCP.

Baseado na especificação oficial: https://ucp.dev/specification/overview/

O manifest é publicado em /.well-known/ucp e declara:
- Versão do protocolo
- Services disponíveis (REST, MCP, A2A)
- Capabilities suportadas (checkout, fulfillment, etc.)
- Payment handlers configurados
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


# =========================================================
# Transport Endpoints
# =========================================================

class RESTEndpoint(BaseModel):
    """Configuração de endpoint REST."""
    schema_url: Optional[str] = Field(None, alias="schema")
    endpoint: str

    class Config:
        populate_by_name = True


class MCPEndpoint(BaseModel):
    """Configuração de endpoint MCP (Model Context Protocol)."""
    schema_url: Optional[str] = Field(None, alias="schema")
    endpoint: str

    class Config:
        populate_by_name = True


class A2AEndpoint(BaseModel):
    """Configuração de endpoint A2A (Agent-to-Agent)."""
    endpoint: str


class EmbeddedEndpoint(BaseModel):
    """Configuração de embedded checkout protocol."""
    schema_url: Optional[str] = Field(None, alias="schema")
    delegations: List[str] = []  # ["payment.credential", "fulfillment.address_change"]

    class Config:
        populate_by_name = True


# =========================================================
# Service Definition
# =========================================================

class UCPService(BaseModel):
    """
    Definição de um serviço UCP.

    Exemplo: dev.ucp.shopping
    """
    version: str
    spec: Optional[str] = None
    rest: Optional[RESTEndpoint] = None
    mcp: Optional[MCPEndpoint] = None
    a2a: Optional[A2AEndpoint] = None
    embedded: Optional[EmbeddedEndpoint] = None

    def get_preferred_transport(self) -> Optional[str]:
        """Retorna o transport preferido (MCP > REST > A2A)."""
        if self.mcp:
            return "mcp"
        elif self.rest:
            return "rest"
        elif self.a2a:
            return "a2a"
        return None

    def get_endpoint(self, transport: str) -> Optional[str]:
        """Retorna endpoint para o transport especificado."""
        if transport == "mcp" and self.mcp:
            return self.mcp.endpoint
        elif transport == "rest" and self.rest:
            return self.rest.endpoint
        elif transport == "a2a" and self.a2a:
            return self.a2a.endpoint
        return None


# =========================================================
# Capability Definition
# =========================================================

class UCPCapability(BaseModel):
    """
    Definição de uma capability UCP.

    Capabilities são features dentro de um service.
    Exemplos:
    - dev.ucp.shopping.checkout
    - dev.ucp.shopping.fulfillment
    - dev.ucp.shopping.discount
    """
    name: str  # "dev.ucp.shopping.checkout"
    version: str
    spec: Optional[str] = None
    schema_url: Optional[str] = Field(None, alias="schema")
    extends: Optional[str] = None  # Capability pai

    class Config:
        populate_by_name = True
        extra = "ignore"

    @property
    def short_name(self) -> str:
        """Retorna nome curto para uso como tool name."""
        # dev.ucp.shopping.checkout -> shopping_checkout
        parts = self.name.split(".")
        if len(parts) >= 3:
            return "_".join(parts[2:])  # Remove "dev.ucp."
        return self.name.replace(".", "_")

    @property
    def tool_name(self) -> str:
        """Retorna nome formatado para LangChain tool."""
        return f"ucp_{self.short_name}"

    @property
    def is_extension(self) -> bool:
        """Verifica se é uma extensão de outra capability."""
        return self.extends is not None


# =========================================================
# Payment Configuration
# =========================================================

class UCPPaymentHandler(BaseModel):
    """
    Configuração de um payment handler.

    Define como processar pagamentos para esta loja.
    """
    id: str
    name: str  # "com.example.business_tokenizer"
    version: str
    spec: Optional[str] = None
    config_schema: Optional[str] = None
    instrument_schemas: List[str] = []
    config: Dict[str, Any] = {}


class UCPPaymentConfig(BaseModel):
    """Configuração de pagamentos da loja."""
    handlers: List[UCPPaymentHandler] = []


# =========================================================
# Signing Keys (Security)
# =========================================================

class UCPSigningKey(BaseModel):
    """Chave pública para verificação de assinaturas."""
    kid: str  # Key ID
    kty: str  # Key Type (EC, RSA)
    crv: Optional[str] = None  # Curve (P-256)
    x: Optional[str] = None
    y: Optional[str] = None
    use: str = "sig"
    alg: str = "ES256"


# =========================================================
# Main Manifest
# =========================================================

class UCPCore(BaseModel):
    """Core do manifest UCP (campo 'ucp')."""
    version: str
    services: Dict[str, Any] = {}  # Parseado manualmente para flexibilidade
    capabilities: List[Dict[str, Any]] = []

    @field_validator("capabilities", mode="before")
    @classmethod
    def normalize_capabilities(cls, v: Any) -> List[Dict[str, Any]]:
        """
        Aceita capabilities como lista OU dicionário.

        Formato lista (original):
            [{"name": "dev.ucp.shopping.checkout", "version": "1.0", ...}]

        Formato dicionário (variante de spec real):
            {"dev.ucp.shopping.checkout": [{"version": "1.0", ...}]}
            ou
            {"dev.ucp.shopping.checkout": {"version": "1.0", ...}}
        """
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            result = []
            for cap_name, cap_data in v.items():
                entry: Dict[str, Any] = {"name": cap_name}
                # Valor pode ser uma lista (spec real) ou dict
                if isinstance(cap_data, list) and cap_data:
                    # Pega o primeiro item da lista
                    first = cap_data[0]
                    if isinstance(first, dict):
                        entry.update(first)
                elif isinstance(cap_data, dict):
                    entry.update(cap_data)
                # Garantir version default se ausente
                if "version" not in entry:
                    entry["version"] = "1.0"
                result.append(entry)
            return result
        return []



class UCPManifest(BaseModel):
    """
    Manifest UCP completo.

    Publicado em: https://loja.com/.well-known/ucp

    Estrutura:
    {
        "ucp": {
            "version": "2026-01-11",
            "services": { ... },
            "capabilities": [ ... ]
        },
        "payment": { ... },
        "signing_keys": [ ... ]
    }
    """
    ucp: UCPCore
    payment: Optional[UCPPaymentConfig] = None
    signing_keys: List[UCPSigningKey] = []

    # Metadata (não faz parte do spec, uso interno)
    _store_url: Optional[str] = None
    _fetched_at: Optional[datetime] = None

    class Config:
        underscore_attrs_are_private = True

    @property
    def version(self) -> str:
        """Versão do protocolo UCP."""
        return self.ucp.version

    def get_services(self) -> Dict[str, UCPService]:
        """
        Retorna services parseados.

        Suporta formato real onde cada service é uma lista de transports:
            {"dev.ucp.shopping": [{transport: "mcp", endpoint: ...}, ...]}
        """
        result = {}
        for name, data in self.ucp.services.items():
            try:
                if isinstance(data, list):
                    # Formato real: lista de transports
                    service_kwargs: Dict[str, Any] = {"version": "1.0"}
                    for transport_entry in data:
                        if not isinstance(transport_entry, dict):
                            continue
                        transport_type = transport_entry.get("transport", "")
                        version = transport_entry.get("version", "1.0")
                        service_kwargs["version"] = version
                        if transport_type == "mcp":
                            service_kwargs["mcp"] = MCPEndpoint(
                                endpoint=transport_entry.get("endpoint", ""),
                                schema_url=transport_entry.get("schema")
                            )
                        elif transport_type == "rest":
                            service_kwargs["rest"] = RESTEndpoint(
                                endpoint=transport_entry.get("endpoint", ""),
                                schema_url=transport_entry.get("schema")
                            )
                        elif transport_type == "a2a":
                            service_kwargs["a2a"] = A2AEndpoint(
                                endpoint=transport_entry.get("endpoint", "")
                            )
                        elif transport_type == "embedded":
                            service_kwargs["embedded"] = EmbeddedEndpoint(
                                schema_url=transport_entry.get("schema")
                            )
                        if "spec" in transport_entry:
                            service_kwargs["spec"] = transport_entry["spec"]
                    result[name] = UCPService(**service_kwargs)
                elif isinstance(data, dict):
                    # Formato original: objeto direto
                    result[name] = UCPService(**data)
            except Exception:
                pass  # Skip malformed services
        return result

    def get_capabilities(self) -> List[UCPCapability]:
        """Retorna capabilities parseadas."""
        result = []
        for cap_data in self.ucp.capabilities:
            try:
                result.append(UCPCapability(**cap_data))
            except Exception:
                pass  # Skip malformed capabilities
        return result

    def get_shopping_service(self) -> Optional[UCPService]:
        """Retorna o service de shopping (mais comum)."""
        services = self.get_services()
        return services.get("dev.ucp.shopping")

    def get_preferred_transport(self) -> Optional[str]:
        """Retorna transport preferido do service principal."""
        shopping = self.get_shopping_service()
        if shopping:
            return shopping.get_preferred_transport()
        # Fallback: primeiro service disponível
        for service in self.get_services().values():
            transport = service.get_preferred_transport()
            if transport:
                return transport
        return None

    def supports_capability(self, capability_name: str) -> bool:
        """Verifica se loja suporta uma capability específica."""
        for cap in self.get_capabilities():
            if cap.name == capability_name or cap.short_name == capability_name:
                return True
        return False

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """
        Retorna definições de tools para o editor de prompt.

        Formato compatível com get_ucp_tools_for_prompt().
        """
        tools = []
        for cap in self.get_capabilities():
            tools.append({
                "name": cap.tool_name,
                "description": f"UCP Capability: {cap.name}",
                "type": "ucp",
                "capability": cap.name,
                "version": cap.version,
                "is_extension": cap.is_extension
            })
        return tools


# =========================================================
# Discovery Response
# =========================================================

class UCPDiscoveryResult(BaseModel):
    """Resultado do discovery de uma loja."""
    success: bool
    store_url: str
    manifest: Optional[UCPManifest] = None
    error: Optional[str] = None
    cached: bool = False
    discovered_at: datetime = Field(default_factory=datetime.utcnow)
    # País (ISO-3166 alpha-2) do tenant (agente/loja), quando a config da conexão
    # o declara. Alimenta o envelope UCP da busca de produtos; ``None`` => fallback
    # 'BR' com aviso (BAIXO-006).
    address_country: Optional[str] = None

    @property
    def capabilities_count(self) -> int:
        if self.manifest:
            return len(self.manifest.get_capabilities())
        return 0

    @property
    def preferred_transport(self) -> Optional[str]:
        if self.manifest:
            return self.manifest.get_preferred_transport()
        return None
