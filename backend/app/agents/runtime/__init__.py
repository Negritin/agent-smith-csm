"""
Tool Runtime Core — interfaces base do runtime de tools do Agent.

Exporta as fundações sobre as quais o Registry, os Adapters e o tool_node são
construídos:

- ToolExecutionContext: contexto canônico de execução.
- ToolResult: resultado canônico de qualquer tool.
- AgentTool: interface base (independente do LangChain) dos Adapters.
- LangChainToolShim: ponte interna para llm.bind_tools(...).
- ToolRegistry: fonte única de verdade (discovery, fingerprint, cache, bind).
"""

from .base import AgentTool, LangChainToolShim
from .context import ToolExecutionContext
from .registry import (
    CACHE_TTL_SECONDS,
    MAX_TOOL_CONTENT_BYTES,
    ContextMissingError,
    DiscoverySnapshot,
    DownstreamError,
    ToolBuilder,
    ToolContextLeakError,
    ToolRegistry,
    get_tool_registry,
)
from .result import ToolErrorKind, ToolResult

__all__ = [
    "ToolExecutionContext",
    "ToolResult",
    "ToolErrorKind",
    "AgentTool",
    "LangChainToolShim",
    "ToolRegistry",
    "get_tool_registry",
    "DiscoverySnapshot",
    "ToolBuilder",
    "ToolContextLeakError",
    "ContextMissingError",
    "DownstreamError",
    "CACHE_TTL_SECONDS",
    "MAX_TOOL_CONTENT_BYTES",
]
