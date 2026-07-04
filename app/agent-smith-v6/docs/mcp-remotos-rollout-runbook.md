# Runbook — Rollout dos MCPs Oficiais Remotos (staging)

> Sprint E1 (convergência). Executado por HUMANO em **staging, com contas
> reais**, na ordem da SPEC impl §7 (Fases 0 e 3–6):
> **Gate Fase 0 → Notion → Sentry → Klaviyo → Supabase → Higgsfield**.
>
> A seção do Supabase incorpora a avaliação read-only feita na sprint F4.

## Regra de ouro

**Nenhum server remoto fica ativo sem passar pelo gate.** O seed
(`backend/scripts/seed_mcp_servers.py`) cria os 5 remotos com
`is_active=False`. A ativação por provider acontece **exclusivamente** via
`backend/scripts/activate_mcp_remote_server.py` (dry-run por default,
`--apply` explícito), invocado por humano **após** o checkmark completo do
provider neste runbook:

```bash
cd backend
.venv/bin/python scripts/activate_mcp_remote_server.py notion          # dry-run
.venv/bin/python scripts/activate_mcp_remote_server.py notion --apply  # ativa
```

Pré-condições de QUALQUER ativação: **(1)** gate da Fase 0 aprovado para o
provider; **(2)** checklist de smoke do provider 100% verde; **(3)** linha
preenchida na tabela de status; **(4)** mitigação de prompt injection do
design §7 fiada e verificada (item abaixo). Provider que falhar no spike
**SAI DA FILA** (design §9; precedentes: Figma e ClickUp) e permanece
`is_active=False`.

### Gate obrigatório — prompt injection (design §7)

Output de MCP remoto entra no prompt do LLM vindo de servidor de TERCEIRO e
é tratado como conteúdo não confiável. A fiação implementada (verificar nos
arquivos antes de QUALQUER ativação, e nos testes
`tests/agents/tools/test_mcp_factory_golden.py` /
`tests/services/test_remote_mcp_service.py`):

- [ ] `RemoteMCPService.call_mcp_tool` retorna o payload com
      `untrusted_content=True` e aplica o cap de 100k chars com marcador de
      truncamento (`backend/app/services/remote_mcp_service.py`).
- [ ] `MCPFactoryTool.execute` consome a flag e materializa o `ToolResult`
      com `requires_prompt_safety=True` + `wrap_xml_tag="mcp_remote_result"`
      (`backend/app/agents/tools/mcp_factory.py`) — o Runtime aplica
      `enforce_prompt_safety` e envolve o conteúdo
      (`backend/app/agents/runtime/registry.py`).
- [ ] Cap de 1k chars em `description` persistida no discovery (design §7).

Se qualquer item acima regredir, NENHUM provider remoto pode ser ativado.

---

## Tabela de status do rollout (preencher durante a execução)

| Provider | Spike (Fase 0) ok | Smoke staging ok | Ativado (`--apply`) | Data | Quem |
|---|---|---|---|---|---|
| notion | ☐ | ☐ | ☐ | | |
| sentry | ☐ | ☐ | ☐ | | |
| klaviyo | ☐ | ☐ | ☐ | | |
| supabase | ☐ | ☐ | ☐ | | |
| higgsfield | ☐ | ☐ | ☐ | | |

---

## GATE FASE 0 — Spike (pré-condição de QUALQUER ativação)

Rodar **antes de qualquer smoke/ativação**, com contas reais e browser:

```bash
cd backend
.venv/bin/python scripts/spike_remote_mcp.py              # os 5 endpoints
.venv/bin/python scripts/spike_remote_mcp.py notion       # ou um por vez
```

O script (B1) roda, por endpoint: discovery RFC 9728/8414 → DCR (RFC 7591)
→ authorize PKCE no browser → exchange → `tools/list` via SDK `mcp`, e
imprime contagem de tools + dump dos `input_schema`s.

### Checklist do gate

- [ ] **Spike rodado contra os 5 endpoints** (notion, sentry, klaviyo,
      supabase, higgsfield) e o RESUMO DO GATE registrado abaixo.
- [ ] **Contagem de tools + schemas por provider anotados** (colar o dump
      JSON do spike em anexo/PR — é o insumo do item seguinte).
- [ ] **Validar `_create_input_model`**
      (`backend/app/agents/tools/mcp_factory.py:221`) **contra os schemas
      REAIS do Notion** (design §8.7): objects viram `dict` raso e arrays só
      de primitivos — confirmar que nenhum schema profundo do Notion quebra
      a factory ANTES de qualquer ativação. Forma sugerida: alimentar os
      `input_schema`s do dump num teste local de `_create_input_model` e
      verificar que todos os modelos Pydantic são criados sem erro.
- [ ] **Notion — rotação de refresh token CONFIRMADA na prática**: access
      token expira em 1h e usar um refresh token invalida o anterior. É o
      que justifica o lock por conexão (`mcp:refresh:{agent_id}:{server_id}`
      em `app/services/mcp_remote_oauth.py`).
- [ ] **Higgsfield — DCR CONFIRMADO** (RFC 7591): só inferido em docs
      (funciona como custom connector no claude.ai). Se o DCR falhar,
      avaliar fallback `MCP_HIGGSFIELD_CLIENT_ID/SECRET` no env ou tirar o
      provider da fila.

### Regra de escopo (design §9)

- [ ] Qualquer provider que **falhar no spike SAI DA FILA**: não entra no
      smoke, não é ativado, permanece `is_active=False` no seed. Registrar
      a falha e o motivo na tabela de status.

### Resultado do gate (preencher)

| Provider | DCR ok | tools/list ok | Nº tools | Schemas ok na factory | Veredito |
|---|---|---|---|---|---|
| notion | | | | | |
| sentry | | | | | |
| klaviyo | | | | | |
| supabase | | | | | |
| higgsfield | | | | | |

---

## Smoke por provider — roteiro base (template = Notion)

Cada provider abaixo repete este roteiro end-to-end em staging (UI nova da
F3: página `app/admin/agent/[agentId]?section=mcp`, card do server na seção
Ferramentas), mais os quirks específicos. Marcar TODOS os itens antes da
ativação.

### 1. Notion (Fase 3 — template dos demais)

- [ ] **Spike ok** (gate acima) — inclui rotação de refresh token confirmada.
- [ ] **Conectar conta real via UI**: card do Notion → "Conectar" → popup
      OAuth → autorizar workspace → popup fecha com `MCP_OAUTH_SUCCESS`.
- [ ] **`connection_metadata` exibido**: o card mostra o workspace/conta
      conectado deste agente (isolamento visível na UI — design §5.3.6).
- [ ] **Discovery automático pós-callback**: lista completa de tools aparece
      no card, **todas OFF por padrão** (`is_enabled=false`), contador
      "0 de Y habilitadas".
- [ ] **Curadoria**: habilitar 2–3 tools (ex.: busca + leitura de página) e
      verificar persistência (reload da página mantém os toggles).
- [ ] **Execução via agente**: conversa real com o agente usando uma tool
      habilitada (ex.: "busque X no Notion") → resposta usa o resultado do
      MCP; tool NÃO habilitada não aparece para o LLM.
- [ ] **Conteúdo não confiável (gate §7)**: na execução acima, confirmar nos
      logs/trace que o ToolResult remoto veio com
      `requires_prompt_safety=True` e o conteúdo envolto em
      `<mcp_remote_result>` (fiação `untrusted_content` →
      `MCPFactoryTool`). Sem isso, NÃO ativar.
- [ ] **"Atualizar tools"** (`POST /agent/{agent_id}/refresh-tools/{mcp_server_id}`):
      re-discovery preserva `is_enabled` das tools curadas.
- [ ] **Refresh de token + rotação (quirk Notion)**: aguardar expiração do
      access token (1h) ou forçar `token_expires_at` no passado; executar
      **2 execuções consecutivas** após expirar e validar que ambas passam
      — o lock `mcp:refresh` serializa o refresh e a rotação não invalida a
      conexão (sem erro de refresh token reutilizado nos logs).
- [ ] **Desconectar/reconectar**: desconectar limpa tokens e o estado do
      card; reconectar (mesma ou outra conta) refaz discovery com tools OFF.
- [ ] **Ativação**: `activate_mcp_remote_server.py notion --apply` +
      preencher tabela de status.

### 2. Sentry (Fase 4)

- [ ] Spike ok (gate acima).
- [ ] Roteiro base completo (conectar conta real, `connection_metadata`
      com a organização Sentry, discovery com tools OFF, curadoria 2–3
      tools — ex.: busca de issues —, execução via agente, "Atualizar
      tools", refresh de token, desconectar/reconectar).
- [ ] Ativação: `activate_mcp_remote_server.py sentry --apply` + tabela.

### 3. Klaviyo (Fase 4)

- [ ] Spike ok (gate acima).
- [ ] **Quirk — papel da conta**: a autorização só funciona para
      **Owner/Admin/Manager** da conta Klaviyo. Testar com conta de papel
      insuficiente e validar a mensagem amigável na UI: "a conta usada
      precisa ser Owner, Admin ou Manager no Klaviyo"
      (`components/admin/agent-config/McpServerCard.tsx`). Nota da F4: para
      o mapeamento cobrir o fluxo do popup, o callback OAuth do backend
      deve emitir `postMessage({type: 'MCP_OAUTH_ERROR', provider, error})`
      no caso de falha (hoje só emite `MCP_OAUTH_SUCCESS`) — alinhar com a
      track backend antes deste smoke.
- [ ] Roteiro base completo com conta de papel suficiente (conectar,
      `connection_metadata`, discovery OFF, curadoria, execução via agente,
      "Atualizar tools", refresh de token, desconectar/reconectar).
- [ ] Ativação: `activate_mcp_remote_server.py klaviyo --apply` + tabela.

### 4. Supabase (Fase 5 — mais sensível do lote)

**Pré-requisito de ativação:** a decisão read-only da F4 (abaixo) aplicada —
toggle read-only na UI + allowlist do PATCH config + serialização lowercase
de booleans verificadas.

- [ ] Spike ok (gate acima).
- [ ] **Quirk — `project_ref` OBRIGATÓRIO**: o card exige `project_ref`
      (salvo via `PATCH /agent/{agent_id}/connection/{mcp_server_id}/config`)
      **antes de liberar os toggles de tools**; conferir que a URL final do
      MCP recebe `?project_ref=<id>` (restringe a um projeto, desabilita
      account tools).
- [ ] **Quirk — decisão read-only (F4)**: toggle "Modo somente leitura"
      ligado adiciona `read_only=true` à URL; validar com uma query de
      escrita que o servidor a REJEITA em modo read-only.
- [ ] **Aviso de escrita VISÍVEL**: o card exibe, em qualquer estado, o
      aviso fixo "este MCP pode executar SQL com escrita no seu projeto".
- [ ] Roteiro base completo (conectar, `connection_metadata`, discovery
      OFF, curadoria, execução via agente — ex.: query read-only —,
      "Atualizar tools", refresh de token, desconectar/reconectar).
- [ ] Ativação: `activate_mcp_remote_server.py supabase --apply` + tabela.

#### Avaliação do modo read-only (F4, 2026-06-12) — DECISÃO: suportado e exposto na UI

**Pergunta:** o MCP oficial hospedado do Supabase (`https://mcp.supabase.com/mcp`)
suporta modo read-only?

**Resposta: SIM.** Conforme a documentação oficial
(https://supabase.com/docs/guides/getting-started/mcp), o servidor hospedado
aceita os seguintes query params na URL:

| Param | Efeito |
|---|---|
| `project_ref=<id>` | Restringe a um único projeto (desabilita account tools) |
| `read_only=true` | Executa todas as queries como usuário Postgres read-only |
| `features=<grupos>` | Habilita apenas grupos de tools (csv, ex.: `database,docs`) |

Exemplo combinado: `https://mcp.supabase.com/mcp?project_ref=abc123&read_only=true`.
A própria documentação recomenda read-only como best practice ao conectar em
dados reais.

**Decisão (F4):** a opção é exposta no card do Supabase (seção Ferramentas)
como toggle booleano "Modo somente leitura (read-only)", persistida em
`agent_mcp_connections.connection_config.read_only` via
`PATCH /agent/{agent_id}/connection/{mcp_server_id}/config` (mesmo PATCH
genérico do `project_ref`). A montagem da URL final já aplica
`connection_config` como query params no `RemoteMCPService._build_url`
(backend/app/services/remote_mcp_service.py) — nenhuma mudança estrutural é
necessária no backend além da allowlist.

**Alinhamento com a track backend (B5, SPEC impl §4.3) — IMPLEMENTADO
(2026-06-12, correção pós-validação final):**

1. ✅ A allowlist de chaves do PATCH config do Supabase aceita `read_only`
   (boolean estrito, opcional) além de `project_ref` (string
   `^[a-z0-9]{15,25}$`, obrigatório) — `_CONNECTION_CONFIG_RULES` em
   `backend/app/api/mcp.py`; testes em
   `backend/tests/api/test_mcp_connection_config.py`.
2. ✅ **Serialização:** `urllib.parse.urlencode({"read_only": True})`
   produziria `read_only=True` (capitalizado, via `str(bool)`); a doc do
   Supabase usa `read_only=true`. O `_build_url` do `RemoteMCPService`
   normaliza booleans para `true`/`false` minúsculos antes do `urlencode` —
   testes em `backend/tests/services/test_remote_mcp_service.py`
   (`test_read_only_*_serializado_lowercase_na_url`).

No smoke da Fase 5, validar na prática (query de escrita rejeitada) que o
servidor do Supabase aplicou o modo read-only.

### 5. Higgsfield (Fase 6 — condicionado à Fase 0)

- [ ] **Spike ok COM DCR confirmado** (gate acima). Se o DCR falhou e não
      houver fallback viável (`MCP_HIGGSFIELD_CLIENT_ID/SECRET`), o provider
      SAI DA FILA — não prosseguir.
- [ ] Roteiro base completo (conectar conta real, `connection_metadata`,
      discovery OFF, curadoria, execução via agente — ex.: geração de
      imagem —, "Atualizar tools", refresh de token, desconectar/reconectar).
- [ ] Ativação: `activate_mcp_remote_server.py higgsfield --apply` + tabela.

---

## Pós-rollout

- [ ] Tabela de status 100% preenchida (incluindo providers que saíram da
      fila, com motivo).
- [ ] `.env.example` atualizado se algum provider exigiu override
      `MCP_<PROVIDER>_CLIENT_ID/SECRET` (fallback de DCR).
- [ ] `graphify update .` rodado na raiz do projeto (regra do repo).
