#!/usr/bin/env python3
"""Standalone regression check for SUP-MCP-020."""

from __future__ import annotations

import json
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "backend" / "app"
SERVICES_DIR = APP_DIR / "services"

app_pkg = types.ModuleType("app")
app_pkg.__path__ = [str(APP_DIR)]
services_pkg = types.ModuleType("app.services")
services_pkg.__path__ = [str(SERVICES_DIR)]
sys.modules.setdefault("app", app_pkg)
sys.modules.setdefault("app.services", services_pkg)


def load_service_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


load_service_module(
    "app.services.mcp_log_utils",
    SERVICES_DIR / "mcp_log_utils.py",
)
mcp_gateway_service = load_service_module(
    "app.services.mcp_gateway_service",
    SERVICES_DIR / "mcp_gateway_service.py",
)

_serialize_mcp_jsonrpc_request = mcp_gateway_service._serialize_mcp_jsonrpc_request


def expect_reject(payload: dict[str, Any], label: str) -> None:
    try:
        _serialize_mcp_jsonrpc_request(payload)
    except ValueError:
        return
    raise AssertionError(f"{label} should have been rejected")


valid_call = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
        "name": "calendar.list_events",
        "arguments": {"limit": 5},
    },
}

encoded = _serialize_mcp_jsonrpc_request(valid_call)
decoded = json.loads(encoded.decode("utf-8"))
assert decoded["jsonrpc"] == "2.0"
assert decoded["method"] == "tools/call"
assert isinstance(decoded["params"], dict)

expect_reject(
    {"jsonrpc": "2.0", "id": 1, "method": "tools/delete", "params": {}},
    "unexpected method",
)
expect_reject(
    {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": [], "extra": True},
    "unexpected top-level field",
)
expect_reject(
    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x", "env": {}}},
    "dangerous params key",
)
expect_reject(
    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "bad tool", "arguments": {}}},
    "invalid tool name",
)
expect_reject(
    {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"unexpected": True}},
    "non-empty tools/list params",
)

print("SUP-MCP-020 passed")
