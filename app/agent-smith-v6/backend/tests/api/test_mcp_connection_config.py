"""
Testes da validação de connection_config (api/mcp.py — SPEC impl §4.3 +
decisão read-only da F4 no runbook docs/mcp-remotos-rollout-runbook.md).

Contrato F4×B5: o card do Supabase (McpServerCard.tsx) SEMPRE envia
{project_ref, read_only} no PATCH /agent/{agent_id}/connection/
{mcp_server_id}/config — o backend deve aceitar exatamente esse shape.
Função pura, sem DB (os guards de tenant do endpoint são cobertos em
tests/security/test_mcp_remote_isolation.py).
"""

from __future__ import annotations

import pathlib
import sys
import types

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _install_api_package() -> None:
    """Permite `import app.api.mcp` sem executar app/api/__init__.py.

    Na suíte completa, os conftests de tests/agents registram pacotes
    sintéticos de app.services que quebram o import transitivo do __init__
    do pacote api (chat -> AudioService). Mesmo padrão de
    tests/security/conftest.py: pacote sintético com __path__ REAL — o
    submódulo app.api.mcp resolve do disco; via setdefault, nunca sombreia
    um app.api já importado de verdade.
    """
    import app  # noqa: F401  (pacote real e leve)

    if "app.api" not in sys.modules:
        package = types.ModuleType("app.api")
        package.__path__ = [str(_BACKEND_ROOT / "app" / "api")]
        package.__package__ = "app.api"
        sys.modules["app.api"] = package
        setattr(app, "api", package)


_install_api_package()

from app.api.mcp import _validate_connection_config  # noqa: E402


# --------------------------------------------------------------------------- #
# Supabase — shape exato enviado pelo frontend (F4)
# --------------------------------------------------------------------------- #
def test_supabase_aceita_payload_do_card_project_ref_e_read_only():
    error = _validate_connection_config(
        "supabase", {"project_ref": "abcdefghij12345", "read_only": True}
    )
    assert error is None


def test_supabase_aceita_read_only_false():
    error = _validate_connection_config(
        "supabase", {"project_ref": "abcdefghij12345", "read_only": False}
    )
    assert error is None


def test_supabase_read_only_e_opcional():
    error = _validate_connection_config(
        "supabase", {"project_ref": "abcdefghij12345"}
    )
    assert error is None


def test_supabase_read_only_nao_booleano_rejeitado():
    # Boolean ESTRITO: "true" string (ou 1) não vale — evita ambiguidade na
    # serialização da URL (read_only=true) feita pelo RemoteMCPService.
    for bad in ("true", "false", 1, 0, "sim"):
        error = _validate_connection_config(
            "supabase", {"project_ref": "abcdefghij12345", "read_only": bad}
        )
        assert error == "Valor inválido para connection_config.read_only"


def test_supabase_project_ref_continua_obrigatorio():
    error = _validate_connection_config("supabase", {"read_only": True})
    assert error == "Campo obrigatório ausente em connection_config: project_ref"


def test_supabase_project_ref_formato_invalido():
    error = _validate_connection_config(
        "supabase", {"project_ref": "INVALIDO!", "read_only": True}
    )
    assert error == "Valor inválido para connection_config.project_ref"


def test_supabase_chave_desconhecida_rejeitada():
    error = _validate_connection_config(
        "supabase",
        {"project_ref": "abcdefghij12345", "features": "database,docs"},
    )
    assert error is not None
    assert "features" in error


# --------------------------------------------------------------------------- #
# Servers fora do mapa: só config vazio
# --------------------------------------------------------------------------- #
def test_server_sem_regras_aceita_config_vazio():
    assert _validate_connection_config("notion", {}) is None


def test_server_sem_regras_rejeita_qualquer_chave():
    error = _validate_connection_config("notion", {"read_only": True})
    assert error is not None
    assert "read_only" in error
