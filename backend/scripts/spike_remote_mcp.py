#!/usr/bin/env python3
"""Spike Fase 0 — OAuth 2.1 + DCR + tools/list nos 5 MCPs remotos oficiais.

Script MANUAL e DESCARTÁVEL (SPEC impl §3.3/§7.1, design §9). Não faz parte do
produto; execução acontece no sprint de convergência E1, com contas reais, e o
output decide o escopo final do rollout remoto.

Para cada endpoint o script roda:
    1. Discovery RFC 9728 (/.well-known/oauth-protected-resource)
       -> RFC 8414 (authorization server metadata).
    2. DCR (RFC 7591) no registration_endpoint (public client + PKCE).
    3. Abre a URL de autorização no browser (webbrowser) e espera o redirect
       num callback local em loopback (http://127.0.0.1:8976/callback).
    4. Exchange do código com PKCE (S256) + resource (RFC 8707).
    5. tools/list via SDK `mcp` (Streamable HTTP) e imprime a CONTAGEM de
       tools + DUMP dos input_schemas em JSON — insumo para validar
       _create_input_model (app/agents/tools/mcp_factory.py) contra schemas
       reais antes da Fase 2.

ITENS DO GATE DA FASE 0 (anotar os resultados):
    - Notion: confirmar na prática a ROTAÇÃO de refresh tokens (access token
      de 1h; usar um refresh token invalida o anterior) — justifica o lock
      por conexão no refresh genérico.
    - Higgsfield: confirmar que o DCR (RFC 7591) funciona — só inferido em
      docs (funciona como custom connector no claude.ai), não confirmado.
    - Regra de escopo: QUALQUER provider que falhar neste spike SAI DA FILA
      (precedentes: Figma caiu em pesquisa, ClickUp caiu na verificação).
      A ativação do seed (is_active=True) por provider depende daqui.

Uso (manual, requer browser e contas reais nos providers):
    cd backend
    .venv/bin/python scripts/spike_remote_mcp.py            # os 5
    .venv/bin/python scripts/spike_remote_mcp.py notion     # um provider
"""

import asyncio
import base64
import hashlib
import json
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlencode, urlparse, urlsplit

import httpx

# NOTA: o SDK `mcp` é importado LAZY dentro de _list_tools() — o pacote pode
# não estar instalado no ambiente (todo import do SDK no projeto é lazy).

CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 8976
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}/callback"
CLIENT_NAME = "Agent Smith (spike Fase 0)"
HTTP_TIMEOUT = 30.0

ENDPOINTS: dict[str, str] = {
    "notion": "https://mcp.notion.com/mcp",
    "klaviyo": "https://mcp.klaviyo.com/mcp",
    "sentry": "https://mcp.sentry.dev/mcp",
    "supabase": "https://mcp.supabase.com/mcp",
    "higgsfield": "https://mcp.higgsfield.ai/mcp",
}


# ---------------------------------------------------------------------------
# 1. Discovery: RFC 9728 -> RFC 8414
# ---------------------------------------------------------------------------
def discover_metadata(server_url: str) -> dict[str, Any]:
    """Resolve o authorization server metadata a partir da URL do MCP."""
    parts = urlsplit(server_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")

    with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        # RFC 9728 — protected resource metadata (variante path-aware primeiro)
        candidates = [
            f"{origin}/.well-known/oauth-protected-resource{path}",
            f"{origin}/.well-known/oauth-protected-resource",
        ]
        resource_meta: dict[str, Any] = {}
        for url in candidates:
            resp = client.get(url)
            if resp.status_code == 200:
                resource_meta = resp.json()
                print(f"  [9728] protected resource metadata: {url}")
                break
        auth_servers = resource_meta.get("authorization_servers") or [origin]
        auth_server = auth_servers[0].rstrip("/")

        # RFC 8414 — authorization server metadata (fallback OIDC discovery)
        as_parts = urlsplit(auth_server)
        as_origin = f"{as_parts.scheme}://{as_parts.netloc}"
        as_path = as_parts.path.rstrip("/")
        candidates = [
            f"{as_origin}/.well-known/oauth-authorization-server{as_path}",
            f"{as_origin}/.well-known/oauth-authorization-server",
            f"{as_origin}/.well-known/openid-configuration",
        ]
        for url in candidates:
            resp = client.get(url)
            if resp.status_code == 200:
                meta = resp.json()
                print(f"  [8414] authorization server metadata: {url}")
                return meta

    raise RuntimeError(f"metadata RFC 8414 não encontrado para {server_url}")


# ---------------------------------------------------------------------------
# 2. DCR (RFC 7591)
# ---------------------------------------------------------------------------
def register_client(metadata: dict[str, Any]) -> dict[str, Any]:
    """Registra o spike como public client via Dynamic Client Registration."""
    registration_endpoint = metadata.get("registration_endpoint")
    if not registration_endpoint:
        raise RuntimeError(
            "provider sem registration_endpoint — DCR indisponível "
            "(candidato a fallback MCP_<PROVIDER>_CLIENT_ID/SECRET ou a "
            "sair da fila)"
        )
    payload = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        resp = client.post(registration_endpoint, json=payload)
        resp.raise_for_status()
        registration = resp.json()
    print(f"  [7591] DCR ok — client_id={registration.get('client_id')}")
    return registration


# ---------------------------------------------------------------------------
# 3. Autorização no browser + callback loopback
# ---------------------------------------------------------------------------
class _CallbackHandler(BaseHTTPRequestHandler):
    """Captura ?code=&state= do redirect num único request."""

    captured: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 (assinatura do BaseHTTPRequestHandler)
        from urllib.parse import parse_qs

        query = parse_qs(urlparse(self.path).query)
        type(self).captured = {k: v[0] for k, v in query.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            "<h1>Spike: autorizado.</h1>Pode fechar esta aba.".encode()
        )

    def log_message(self, *args: Any) -> None:  # silencia o http.server
        return


def authorize_interactive(
    metadata: dict[str, Any],
    client_id: str,
    server_url: str,
) -> tuple[str, str]:
    """Abre o browser e espera o code; retorna (code, code_verifier)."""
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(16)

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "resource": server_url,  # RFC 8707
    }
    auth_url = f"{metadata['authorization_endpoint']}?{urlencode(params)}"

    _CallbackHandler.captured = {}
    server = HTTPServer((CALLBACK_HOST, CALLBACK_PORT), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print(f"  [auth] abrindo browser: {auth_url}")
    webbrowser.open(auth_url)
    print("  [auth] aguardando redirect no callback local...")
    thread.join(timeout=300)
    server.server_close()

    captured = _CallbackHandler.captured
    if "error" in captured:
        raise RuntimeError(f"authorize negado: {captured}")
    if captured.get("state") != state:
        raise RuntimeError("state do callback não confere (ou timeout)")
    return captured["code"], code_verifier


# ---------------------------------------------------------------------------
# 4. Exchange com PKCE
# ---------------------------------------------------------------------------
def exchange_code(
    metadata: dict[str, Any],
    registration: dict[str, Any],
    code: str,
    code_verifier: str,
    server_url: str,
) -> dict[str, Any]:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": registration["client_id"],
        "code_verifier": code_verifier,
        "resource": server_url,  # RFC 8707
    }
    auth = None
    if registration.get("client_secret"):
        auth = (registration["client_id"], registration["client_secret"])
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        resp = client.post(metadata["token_endpoint"], data=data, auth=auth)
        resp.raise_for_status()
        tokens = resp.json()
    has_refresh = "refresh_token" in tokens
    print(
        f"  [token] exchange ok — expires_in={tokens.get('expires_in')!r}, "
        f"refresh_token={'sim' if has_refresh else 'NÃO'}"
    )
    return tokens


# ---------------------------------------------------------------------------
# 5. tools/list via SDK mcp (import LAZY — gate de ambiente)
# ---------------------------------------------------------------------------
async def _list_tools(server_url: str, access_token: str) -> list[Any]:
    # Import lazy: o SDK pode não estar instalado fora do venv do spike.
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {access_token}"}
    async with streamablehttp_client(server_url, headers=headers) as (
        read_stream,
        write_stream,
        _,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.list_tools()
            return list(result.tools)


def dump_tools(provider: str, tools: list[Any]) -> None:
    """Imprime contagem + input_schemas em JSON (insumo do _create_input_model)."""
    print(f"  [tools] {provider}: {len(tools)} tools")
    schemas = [
        {
            "name": tool.name,
            "description": (tool.description or "")[:200],
            "input_schema": tool.inputSchema,
        }
        for tool in tools
    ]
    print(json.dumps({provider: schemas}, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def run_provider(provider: str, server_url: str) -> bool:
    print(f"\n{'=' * 70}\n>> {provider} — {server_url}\n{'=' * 70}")
    try:
        metadata = discover_metadata(server_url)
        registration = register_client(metadata)
        code, verifier = authorize_interactive(
            metadata, registration["client_id"], server_url
        )
        tokens = exchange_code(metadata, registration, code, verifier, server_url)
        tools = asyncio.run(_list_tools(server_url, tokens["access_token"]))
        dump_tools(provider, tools)
        return True
    except Exception as exc:  # noqa: BLE001 — spike: reporta e segue pro próximo
        print(f"  [FALHA] {provider}: {exc}")
        print(f"  [GATE] {provider} candidato a SAIR DA FILA (SPEC impl §7.1)")
        return False


def main() -> int:
    requested = sys.argv[1:] or list(ENDPOINTS)
    unknown = [p for p in requested if p not in ENDPOINTS]
    if unknown:
        print(f"providers desconhecidos: {unknown} — opções: {list(ENDPOINTS)}")
        return 2

    results = {p: run_provider(p, ENDPOINTS[p]) for p in requested}

    print(f"\n{'=' * 70}\nRESUMO DO GATE (Fase 0)\n{'=' * 70}")
    for provider, ok in results.items():
        verdict = "OK — apto a ativar no seed (E1)" if ok else "FALHOU — sai da fila"
        print(f"  {provider:<12} {verdict}")
    print(
        "\nLembrete: validar manualmente a rotação de refresh token do Notion\n"
        "e registrar contagens/schemas acima na decisão de escopo do E1."
    )
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
