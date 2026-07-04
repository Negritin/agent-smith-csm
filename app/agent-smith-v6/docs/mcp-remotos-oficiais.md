# 🔌 MCPs Oficiais Remotos — Notion, Klaviyo, Sentry, Supabase, Higgsfield

> Guia completo da integração com os MCP servers **oficiais remotos** via Streamable HTTP + OAuth 2.1.
> Rollout em staging: [`docs/mcp-remotos-rollout-runbook.md`](mcp-remotos-rollout-runbook.md).

---

## Visão Geral

Além dos 4 servidores MCP **internos** (Google Drive, Google Calendar, GitHub, Slack — subprocess stdio), o Agent Smith conecta agentes aos MCP servers **oficiais remotos** de 5 serviços. Nenhum servidor Python novo foi escrito — as tools são herdadas do que cada servidor expõe via `tools/list`.

| Provider | Endpoint oficial | Observações |
|----------|-----------------|-------------|
| Notion | `https://mcp.notion.com/mcp` | Access token expira em 1h; refresh tokens rotacionados (lock Redis por conexão no refresh) |
| Klaviyo | `https://mcp.klaviyo.com/mcp` | Só autorizável por conta **Owner, Admin ou Manager** |
| Sentry | `https://mcp.sentry.dev/mcp` | — |
| Supabase | `https://mcp.supabase.com/mcp` | Exige `project_ref` por conexão; expõe SQL com escrita (há modo read-only) |
| Higgsfield | `https://mcp.higgsfield.ai/mcp` | Provider menos maduro — ativação condicionada ao spike (Fase 0) |

---

## Como Funciona

### Arquitetura

- **Dispatcher por `server_type`** (`mcp_gateway_service.py`): servidores `internal` seguem o caminho subprocess stdio atual (intocado); servidores `remote` são delegados ao `RemoteMCPService` (`backend/app/services/remote_mcp_service.py`).
- **Sessão Streamable HTTP stateless por chamada** (SDK oficial `mcp`): `initialize` → `tools/list`/`tools/call` → close. Nenhuma sessão, client ou token fica em cache de instância — o token é resolvido por `(agent_id, server)` **a cada chamada**.
- **Mesma interface de retorno do gateway stdio**: o `MCPFactoryTool` (`mcp_factory.py`) não percebe diferença entre interno e remoto.
- Timeout de 60s por chamada, cap de 100k chars na resposta (truncamento com marcador) e redaction de logs compartilhada (`mcp_log_utils.py`).
- **Output remoto é conteúdo não confiável**: o resultado volta com `untrusted_content=True` e o runtime aplica `enforce_prompt_safety` envolvendo-o em `<mcp_remote_result>` (mitigação de prompt injection — design §7).

### OAuth 2.1 + PKCE + DCR (`backend/app/services/mcp_remote_oauth.py`)

1. **Discovery de metadata**: RFC 9728 (`/.well-known/oauth-protected-resource`) → RFC 8414 (authorization server metadata), com cache em `mcp_oauth_clients.auth_metadata`.
2. **Client registration**: **DCR (RFC 7591)** automático na primeira conexão — o backend registra o Agent Smith como app no provider e persiste em `mcp_oauth_clients` (1 registro por servidor, da plataforma). Override opcional por env (`MCP_<PROVIDER>_CLIENT_ID/SECRET`) tem precedência.
3. **PKCE S256 sempre**: o `code_verifier` fica **server-side no Redis** (`mcp:pkce:{nonce}`, TTL 600s, uso único) — nunca transita pelo browser. O `state` é HMAC-assinado com `APP_SECRET` (ou `SECRET_KEY`).
4. **Resource indicator** (RFC 8707): `resource=<url do servidor>` no authorize e no token exchange.
5. **Refresh genérico** (`grant_type=refresh_token` contra o `token_endpoint` do metadata), com **lock Redis por conexão** (`mcp:refresh:{agent_id}:{server_id}`) — necessário porque o Notion rotaciona refresh tokens.

### Tokens por agente, criptografados

Os tokens de usuário são **100% por agente**: cada linha de `agent_mcp_connections` é `(agent_id, mcp_server_id)` com tokens criptografados via `encryption_service` (Fernet, chave `ENCRYPTION_KEY`). Todo read/write valida `company_id`.

### Banco (migration `20260612_mcp_remote_servers.sql`)

| Tabela | Mudança |
|--------|---------|
| `mcp_servers` | `server_type` (`internal`/`remote`), `url`, `extra_headers` |
| `mcp_oauth_clients` (nova) | Registro DCR da plataforma por servidor (client_id, secret criptografado, metadata RFC 8414) |
| `agent_mcp_connections` | `connection_config` jsonb (ex.: `project_ref` do Supabase) |
| `agent_mcp_tools` | `is_available` (tool que sumiu do servidor fica indisponível, **não é deletada** — preserva a curadoria) |

---

## Como Ligar um MCP em um Agente (fluxo do cliente)

**Ninguém digita API key — nem a plataforma, nem o cliente.**

1. Abra a **tela de configuração do agente** (página inteira em `/admin/agent/[agentId]`; o antigo modal foi aposentado) e vá à seção **Ferramentas**.
2. No card do servidor desejado (Notion, Klaviyo, Sentry, Supabase, Higgsfield), clique em **"Conectar"**. Abre um popup com a tela de login **do próprio serviço** — o cliente entra com a conta dele e autoriza.
3. Após o callback, o backend troca o código por token (PKCE), criptografa, grava amarrado ao `(agent_id, mcp_server_id)` e roda o discovery (`tools/list`) automaticamente.
4. As tools aparecem no card **todas desabilitadas (OFF) por padrão** — o cliente liga uma a uma (controle de custo de tokens e de alucinação). Há contador "X de Y habilitadas", busca por nome e botão **"Atualizar tools"** (re-discovery manual; nunca reseta a curadoria).

Tools habilitadas viram variáveis `{mcp_<server>_<tool>}` no prompt do agente, como nos MCPs internos. Tools com `is_available=false` (sumiram do servidor) aparecem acinzentadas com toggle bloqueado.

### Supabase — `project_ref` obrigatório

- O card do Supabase exige o **`project_ref`** do projeto (15–25 chars `a-z0-9`) **antes de liberar os toggles** das tools. Ele é salvo via `PATCH /agent/{agent_id}/connection/{mcp_server_id}/config` e aplicado como `?project_ref=` na URL do servidor, restringindo a conexão a um único projeto.
- Há também o **modo read-only** (`read_only` no `connection_config`).
- A UI mostra aviso fixo: este MCP pode executar **SQL com escrita** no projeto.

### Klaviyo — exigência de papel

A conta usada na autorização precisa ser **Owner, Admin ou Manager** no Klaviyo. Contas com papel insuficiente recebem erro amigável na UI ("a conta usada precisa ser Owner, Admin ou Manager no Klaviyo").

---

## Isolamento por Agente

A conexão é **por agente** (= por cliente). Dois agentes — inclusive da mesma company — podem estar conectados a **duas contas/workspaces diferentes do mesmo serviço** (ex.: dois Notions distintos), cada um com sua própria curadoria de tools. Conectar/habilitar/usar um MCP em um agente **nunca** afeta outro agente ou outra company:

- A identidade de execução vem do `ToolExecutionContext` em runtime; o token é resolvido por `(agent_id, server)` a cada chamada.
- O card na UI sempre exibe de qual conta/workspace é a conexão **daquele** agente.
- Testes de isolamento em `backend/tests/security/test_mcp_remote_isolation.py`.

---

## O Que o Operador da Plataforma Precisa

1. **Migration**: `backend/supabase/migrations/20260612_mcp_remote_servers.sql`.
2. **Seed**: `python scripts/seed_mcp_servers.py` — cria os 5 servidores remotos no catálogo com **`is_active=False`**.
3. **Ativação gateada por provider**: `python scripts/activate_mcp_remote_server.py <provider> --apply` (dry-run por default), **somente** após o checkmark do provider no [runbook](mcp-remotos-rollout-runbook.md) (gate do spike + smoke em staging).
4. **Env**: `APP_SECRET` (assina o `state` HMAC do OAuth) e **Redis** (PKCE verifier + lock de refresh) — ambos já obrigatórios na stack. `ENCRYPTION_KEY` (tokens at-rest) e `MCP_OAUTH_REDIRECT_BASE` também já existentes.
5. **Nenhuma API key de provider**: o client registration é obtido via **DCR automático** na primeira conexão e persistido em `mcp_oauth_clients`. Zero cadastro manual.
6. **Overrides opcionais** (fallback, apenas se o DCR falhar em algum provider): registrar um app manualmente no portal do provider e definir `MCP_<PROVIDER>_CLIENT_ID` / `MCP_<PROVIDER>_CLIENT_SECRET` (ex.: `MCP_NOTION_CLIENT_ID`). Ver bloco comentado em `backend/.env.example`.

### Script de spike (validação de providers)

`backend/scripts/spike_remote_mcp.py` — script manual (Fase 0) que valida cada provider de ponta a ponta **antes** da ativação: discovery RFC 9728/8414 → DCR → autorização no browser (callback local em `127.0.0.1:8976`) → exchange PKCE → `tools/list` com contagem de tools e dump dos schemas.

```bash
cd backend
.venv/bin/python scripts/spike_remote_mcp.py            # os 5 providers
.venv/bin/python scripts/spike_remote_mcp.py notion     # um provider
```

Regra de escopo: provider que falhar no spike **sai da fila** e permanece `is_active=False` (precedentes: Figma e ClickUp caíram em pesquisa/verificação).

---

## Endpoints da API

| Endpoint | Uso |
|----------|-----|
| `POST /agent/{agent_id}/refresh-tools/{mcp_server_id}` | Re-discovery manual (botão "Atualizar tools") |
| `PATCH /agent/{agent_id}/connection/{mcp_server_id}/config` | Salva `connection_config` (ex.: `project_ref`, `read_only`) |
| `GET /oauth/providers` | Data-driven a partir de `mcp_servers` (remotos: `configured=true`, DCR resolve em runtime) |
| `GET /oauth/url/{provider}` / `GET /oauth/callback/{provider}` | Fluxo OAuth (branch 2.1 para remotos; contrato externo inalterado) |

Todos os writes validam company e invalidam graph cache + ToolRegistry.
