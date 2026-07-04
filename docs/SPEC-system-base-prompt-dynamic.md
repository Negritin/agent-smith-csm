# SPEC — System Base Prompt dinâmico (tirar do código → admin master)

> Data: 2026-05-30 · Status: **ENTREGUE** (commit `fe12b70` — `platform_settings_service`, migration, API e UI no admin master). Documento mantido como referência da feature.

---

## 1. Problema

O prompt de governança da plataforma (`SYSTEM_BASE_PROMPT`) é **hardcoded** em
`backend/app/core/prompts.py:14-35`. Ele é injetado em **todo turno de todo agente
de todo cliente** (via `build_composite_prompt` → `graph.py:_build_initial_state:434`),
envolvendo o prompt configurável do cliente e com um rodapé que o torna **prioritário**
sobre as instruções do cliente.

Consequências:
- Para mudar uma vírgula desse prompt, precisa de **deploy** (mexer no código).
- Ninguém de produto/ops consegue iterar; só dev.
- Mudança é global e arriscada (afeta todos os tenants de uma vez) sem nenhuma UI/controle.

## 2. Goal

Mover o `SYSTEM_BASE_PROMPT` para uma **configuração dinâmica de plataforma**, editável
**apenas pelo MASTER ADMIN** via um **menu novo** no admin. O valor inicial é o texto atual
**verbatim** (seed na migration), para o usuário editar e validar iterativamente — **sem deploy**.

Decisões do usuário (fechadas):
- **D-A — Sem fallback hardcoded.** O texto sai 100% do código; passa a viver só no banco.
- **D-B — Valor único editável** (sem histórico/versionamento nesta entrega).
- **D-C — Escopo GLOBAL** (um prompt para toda a plataforma; master admin apenas) — coerente
  com o comportamento atual (o prompt já é global hoje).

Princípios inegociáveis (do projeto): performance, **não-bloqueante**, multi-tenant/concorrência,
código limpo. → o prompt é lido em todo turno, logo **cache obrigatório** (sem query por turno).

---

## 3. Arquitetura da solução

```
[admin master UI]  --PUT-->  [/admin/system-prompt]  --write-->  platform_settings (DB)
                                       |                                 ^
                                       +--invalida cache (Redis)         | (seed migration)
                                                                         |
[turno de chat] _build_initial_state (async)                            |
   -> base = await get_system_base_prompt()  --cache-first(Redis)------>-+
   -> static_prompt = build_composite_prompt(base, client_instructions)  (função PURA)
```

Separação de responsabilidades:
- **I/O async (cache/DB)** fica no caller async (`_build_initial_state`).
- **`build_composite_prompt` vira função PURA** que recebe o `base_prompt` por parâmetro
  (hoje ela lê a constante de módulo). Mais testável, sem I/O.

---

## 4. Data model

Nova tabela **singleton** de configuração de plataforma (key-value, extensível p/ futuras chaves):

```sql
create table if not exists public.platform_settings (
    key         text primary key,
    value       text not null,
    updated_at  timestamptz not null default now(),
    updated_by  uuid  -- admin_users.id do master que editou (auditoria)
);
```

- Linha única usada agora: `key = 'system_base_prompt'`.
- **Sem coluna `company_id`** — é config GLOBAL (D-C).
- RLS: tabela **não** exposta a tenants; acesso só via service-role / endpoint master.

> Por que key-value e não uma coluna dedicada: permite adicionar outras configs globais
> no futuro (ex.: `default_guardrail`, `commerce_instructions`) sem nova migration de schema.

## 5. Migration (seed) — `backend/supabase/migrations/2026XXXX_platform_settings.sql`
1. `create table platform_settings (...)` acima.
2. **Seed** com o `SYSTEM_BASE_PROMPT` atual **verbatim** (copiado de `core/prompts.py:14-35`):
   ```sql
   insert into public.platform_settings (key, value)
   values ('system_base_prompt', $$<TEXTO ATUAL VERBATIM>$$)
   on conflict (key) do nothing;
   ```
   (uso de dollar-quoting `$$...$$` para não escapar aspas/markdown do prompt.)
3. Rollback: `drop table platform_settings;` (aditiva, sem perda de dados de negócio).

---

## 6. Backend

### 6.1 `core/prompts.py` (ALTERA)
- **Remover** a constante `SYSTEM_BASE_PROMPT` (D-A: sai 100% do código).
- `build_composite_prompt(base_prompt: str, client_instructions: str = None) -> str`:
  - agora recebe `base_prompt` por parâmetro (em vez de ler a constante);
  - resto igual (data/hora BR + bloco do cliente + rodapé de prioridade).
- **Atenção:** essa é uma mudança de assinatura — atualizar TODOS os call sites (ver 6.4).

### 6.2 Novo serviço `services/platform_settings_service.py` (NOVO)
```python
# chave de cache + TTL no padrão do billing (billing_service.py)
_CACHE_KEY = "platform:system_base_prompt"
_TTL = 300  # 5 min (backstop; invalidado no save)

async def get_system_base_prompt() -> str:
    # 1) Redis hit -> retorna
    # 2) miss -> lê platform_settings (key='system_base_prompt') -> popula Redis -> retorna
    # 3) DB falha no miss -> usa última cópia em Redis se houver (stale-on-error);
    #    se NÃO houver (cold start + DB down) -> ver Open Question OQ-1.

async def set_system_base_prompt(value: str, updated_by: str) -> None:
    # valida não-vazio -> upsert no DB -> INVALIDA o cache (delete _CACHE_KEY)
```
- **Performance:** leitura cache-first (Redis async). Sem query por turno.
- **Resiliência:** stale-on-error (igual билling fail-closed pattern).
- Opcional: **warm** do cache no startup (`main.py`), como já é feito com o pricing cache.

### 6.3 API master — `api/admin_system_prompt.py` (NOVO) ou dentro de um router admin existente
- `GET  /admin/system-prompt` → `{ value, updated_at, updated_by }` (master only)
- `PUT  /admin/system-prompt` body `{ value }` → **REGRA OBRIGATÓRIA: não-vazio** (ver R1), salva, invalida cache
- Ambos `Depends(require_master_admin)` (já existe em `core/auth.py:182`).
- **Sem endpoint DELETE** (a linha não pode ser apagada — protege o D-A de virar agente sem prompt).

#### R1 — Regra: system prompt NUNCA pode ser vazio (hard rule, server + client)
- **Servidor (autoridade):** o `PUT` faz `value = value.strip()`; se `len(value) == 0` (ou só
  whitespace) → **rejeita com `400`** e mensagem clara (`"O system prompt não pode ficar vazio."`).
  **Não** grava, **não** invalida cache. É a fonte da verdade — mesmo que o front falhe, o vazio nunca entra.
- **Opcional (recomendado):** `CHECK (length(btrim(value)) > 0)` na coluna `value` da tabela, como
  guarda final no banco.
- Combinado com "sem DELETE" + seed, isso garante que **sempre** existe um prompt válido no banco.

### 6.4 Call sites de `build_composite_prompt` (ALTERA)
- `graph.py:434` (caminho VIVO): 
  ```python
  base = await get_system_base_prompt()
  static_prompt = build_composite_prompt(base, base_instructions)
  ```
- Conferir o fallback em `nodes.py:188 build_system_prompt` (caminho legado/fallback) — alinhar
  ou marcar como morto (ver Limpeza, §10).
- Conferir `langchain_service.py:56 DEFAULT_SYSTEM_PROMPT` — **código morto** (definido, nunca usado);
  remover nesta refatoração.

---

## 7. Frontend (admin master)
- Nova página `app/admin/system-prompt/page.tsx` (Next.js), no grupo `app/admin/` já existente.
- UI: textarea grande (monospace), botão **Salvar**, indicador de `updated_at`/`updated_by`.
- Rota BFF `app/api/admin/system-prompt/route.ts` (GET/PUT) → chama o backend master.
- **Visibilidade do menu:** item na sidebar do admin **só para master admin** (mesma checagem
  de role já usada nas outras páginas admin-master).

#### R1 (client) — Bloquear salvar vazio
- Botão **Salvar desabilitado** quando o textarea está vazio/só-whitespace.
- Se mesmo assim disparar, exibir erro inline (`"O system prompt não pode ficar vazio."`) e não chamar a API.
- Espelha a regra do servidor (§6.3 R1), que é a autoridade.

#### R2 — Popup de confirmação OBRIGATÓRIO antes de salvar
- Ao clicar **Salvar**, abrir um **modal de confirmação** (não salva direto):
  > **⚠️ Atenção**
  > Este system prompt afeta **TODOS os agentes do sistema**. Altere com cautela.
  > [ Cancelar ]   [ Confirmar alteração ]
- Só dispara o `PUT` após **Confirmar**. **Cancelar** fecha sem salvar.
- Texto exato do aviso: *"Este system prompt afeta todos os agentes do Sistema, altere com cautela."*

---

## 8. Testes
- **Unit `build_composite_prompt`**: recebe `base_prompt` por param; monta base+data+cliente+rodapé;
  cliente vazio → "Seja um assistente útil e cordial.".
- **Unit `platform_settings_service`**: cache hit não bate no DB; miss lê DB e popula; `set` invalida;
  stale-on-error serve a cópia do Redis; valida não-vazio no `set`.
- **API (R1)**: GET/PUT exigem master admin (403 sem); **PUT vazio/whitespace → 400 e NÃO grava nem invalida cache**; PUT válido grava e invalida cache.
- **Frontend (R1)**: botão Salvar desabilitado com textarea vazio; não chama API no vazio.
- **Frontend (R2)**: clicar Salvar abre o modal de confirmação; **Cancelar** não chama a API;
  **Confirmar** dispara o PUT. (teste do fluxo de confirmação)
- **Integração leve**: `_build_initial_state` usa o valor do serviço (mock) e injeta no SystemMessage.
- **Regressão**: nenhum `SYSTEM_BASE_PROMPT`/`DEFAULT_SYSTEM_PROMPT` restante no código (grep no teste).

---

## 9. Riscos
| Risco | Sev | Mitigação |
|---|---|---|
| **Sem fallback no código (D-A):** linha sumir/DB cair → agente sem governança | Alta | Seed na migration + **sem endpoint DELETE** + validação não-vazio no PUT + cache **stale-on-error** (última cópia boa). Cold-start+DB-down = OQ-1. |
| Prompt lido por turno vira gargalo | Alta | Cache Redis (sem query/turno); warm no startup; invalidação só no save. |
| Mudança global silenciosa (master edita e quebra todos) | Média | Aviso na UI + `updated_by`/`updated_at` audit; validação não-vazio. (Histórico/rollback = fora de escopo, D-B). |
| Assinatura de `build_composite_prompt` muda | Média | Atualizar todos os call sites + testes; grep guard no teste. |
| `nodes.py:build_system_prompt` (fallback legado) diverge | Baixa | Alinhar ao novo serviço ou remover se morto (§10). |

## 10. Limpeza (código morto encontrado)
- `langchain_service.py:56 DEFAULT_SYSTEM_PROMPT` — definido, nunca usado → **remover**.
- `nodes.py:188 build_system_prompt` — fallback que duplica um prompt genérico; avaliar se ainda
  é alcançável e alinhar ao serviço novo (ou remover).

## 11. Rollout
1. Migration (cria + seed) aplicada **antes** do deploy do código (senão `get_system_base_prompt`
   acha a linha vazia). Ordem importa.
2. Deploy backend (serviço + API + call sites).
3. Deploy frontend (menu).
4. Validar: editar o prompt no menu → nova mensagem no chat reflete em ≤ TTL/na hora (cache invalidado).

## 12. Open Questions
- **OQ-1 (a decidir):** comportamento em **cold-start + DB indisponível** (cache vazio, sem nenhuma
  cópia). Opções: (a) falhar o turno com erro claro (sem inventar prompt — mais seguro, mas é outage);
  (b) servir base vazio e logar/alertar alto (degrada governança, mas não derruba). Recomendo **(b)**
  com alerta, pois o cache stale-on-error torna esse caso quase impossível na prática.
- **OQ-2:** warm do cache no startup (`main.py`) — incluir? (recomendo sim, custo zero, evita 1ª-latência).
- **OQ-3:** o menu fica em página dedicada `app/admin/system-prompt` ou dentro de `app/admin/settings`?
  (recomendo dedicada — é uma config sensível e separada).
