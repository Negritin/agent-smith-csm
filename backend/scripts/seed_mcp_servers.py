#!/usr/bin/env python3
"""
Seed script para popular a tabela mcp_servers com os servidores internos
e os servidores remotos oficiais (SPEC mcp-remotos 2026-06-12).

Uso:
    cd backend
    python scripts/seed_mcp_servers.py

Pré-requisito:
    A tabela mcp_servers deve existir no banco (com as colunas da migration
    20260612_mcp_remote_servers.sql: server_type, url, extra_headers).

Este script popula a tabela com as configurações dos servidores MCP.
Apenas metadados de configuração - SEM credenciais ou secrets.

GATE DA FASE 0: os 5 servers remotos são seedados com is_active=False.
A ativação (UPDATE is_active=True) é entregável por provider do sprint de
convergência E1, condicionada ao resultado do spike
(scripts/spike_remote_mcp.py): qualquer provider que falhar no spike sai
da fila (SPEC impl §7.1 / design §9).
"""

import os
import sys
from pathlib import Path

# Adiciona o diretório pai ao path para imports
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from dotenv import load_dotenv

    from supabase import create_client
except ImportError as e:
    print(f"❌ Dependência não encontrada: {e}")
    print("   Execute: pip install supabase python-dotenv")
    sys.exit(1)

# Carrega variáveis de ambiente
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ Erro: SUPABASE_URL e SUPABASE_KEY devem estar definidos no .env")
    sys.exit(1)

# Configuração dos MCP Servers
# NOTA: Apenas metadados públicos - sem credenciais
MCP_SERVERS = [
    # ----- Internos (subprocess stdio) -----
    {
        "name": "google-calendar",
        "display_name": "Google Calendar",
        "description": "Gerenciamento de eventos e agenda do Google Calendar",
        "server_type": "internal",
        "package_name": "internal",
        "command": '["python", "-m", "app.mcp_servers.google_calendar_server"]',
        "oauth_provider": "google",
        "oauth_scopes": '["https://www.googleapis.com/auth/calendar.readonly", "https://www.googleapis.com/auth/calendar.events"]',
        "is_active": True
    },
    {
        "name": "google-drive",
        "display_name": "Google Drive",
        "description": "Acesso a arquivos e pastas do Google Drive",
        "server_type": "internal",
        "package_name": "internal",
        "command": '["python", "-m", "app.mcp_servers.google_drive_server"]',
        "oauth_provider": "google",
        "oauth_scopes": '["https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/drive.file"]',
        "is_active": True
    },
    {
        "name": "slack",
        "display_name": "Slack",
        "description": "Envio de mensagens e gerenciamento de canais do Slack",
        "server_type": "internal",
        "package_name": "internal",
        "command": '["python", "-m", "app.mcp_servers.slack_server"]',
        "oauth_provider": "slack",
        "oauth_scopes": '["channels:read", "chat:write", "users:read"]',
        "is_active": True
    },
    {
        "name": "github",
        "display_name": "GitHub",
        "description": "Gerenciamento de repositórios, issues e pull requests",
        "server_type": "internal",
        "package_name": "internal",
        "command": '["python", "-m", "app.mcp_servers.github_server"]',
        "oauth_provider": "github",
        "oauth_scopes": '["repo", "read:user"]',
        "is_active": True
    },
    # ----- Remotos oficiais (Streamable HTTP, OAuth 2.1) -----
    # is_active=False até o gate da Fase 0 (ver docstring): a ativação por
    # provider acontece no sprint E1, após o spike validar DCR + tools/list.
    {
        "name": "notion",
        "display_name": "Notion",
        "description": "Páginas, bancos de dados e busca no workspace Notion",
        "server_type": "remote",
        "url": "https://mcp.notion.com/mcp",
        "oauth_provider": "notion",
        "package_name": "remote",
        "command": "[]",
        "is_active": False
    },
    {
        "name": "klaviyo",
        "display_name": "Klaviyo",
        "description": "Campanhas, listas e métricas de marketing do Klaviyo",
        "server_type": "remote",
        "url": "https://mcp.klaviyo.com/mcp",
        "oauth_provider": "klaviyo",
        "package_name": "remote",
        "command": "[]",
        "is_active": False
    },
    {
        "name": "sentry",
        "display_name": "Sentry",
        "description": "Issues, eventos e projetos de observabilidade do Sentry",
        "server_type": "remote",
        "url": "https://mcp.sentry.dev/mcp",
        "oauth_provider": "sentry",
        "package_name": "remote",
        "command": "[]",
        "is_active": False
    },
    {
        "name": "supabase",
        "display_name": "Supabase",
        "description": "Projetos e SQL do Supabase (requer project_ref na conexão)",
        "server_type": "remote",
        "url": "https://mcp.supabase.com/mcp",
        "oauth_provider": "supabase",
        "package_name": "remote",
        "command": "[]",
        "is_active": False
    },
    {
        "name": "higgsfield",
        "display_name": "Higgsfield",
        "description": "Geração de imagens e vídeos com IA via Higgsfield",
        "server_type": "remote",
        "url": "https://mcp.higgsfield.ai/mcp",
        "oauth_provider": "higgsfield",
        "package_name": "remote",
        "command": "[]",
        "is_active": False
    },
]


def seed_mcp_servers():
    """Popula a tabela mcp_servers com os servidores internos."""
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    print("\n" + "=" * 50)
    print("🔄 Populando tabela mcp_servers...")
    print("=" * 50 + "\n")

    success_count = 0
    error_count = 0

    for server in MCP_SERVERS:
        try:
            # Upsert: insere ou atualiza se já existir
            result = supabase.table("mcp_servers").upsert(
                server,
                on_conflict="name"
            ).execute()

            if result.data:
                print(f"  ✅ {server['display_name']}")
                success_count += 1
            else:
                print(f"  ⚠️ {server['display_name']} - sem retorno")

        except Exception as e:
            print(f"  ❌ {server['display_name']}: {e}")
            error_count += 1

    print("\n" + "=" * 50)
    print(f"📊 Resultado: {success_count} inseridos, {error_count} erros")
    print("=" * 50 + "\n")

    if success_count > 0:
        print("✅ Seed concluído! MCP Servers configurados:")
        for server in MCP_SERVERS:
            status = "ativo" if server["is_active"] else "INATIVO (gate Fase 0)"
            kind = server["server_type"]
            print(f"   - {server['display_name']} [{kind}, {status}]")


if __name__ == "__main__":
    seed_mcp_servers()
