"""
MCP Servers internos do Agent Smith.
Cada server implementa o protocolo MCP (JSON-RPC sobre stdio).
"""

from .base_server import BaseMCPServer

__all__ = ["BaseMCPServer"]
