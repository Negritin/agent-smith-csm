-- S5 (SPEC-whatsapp-uazapi §2.2 / §6.1 / §6.2) — migração datada para a
-- integração uazapi: tornar instance_id nullable, DESATIVAR duplicatas WhatsApp
-- ativas por agente e criar índice ÚNICO PARCIAL "uma integração WhatsApp ATIVA
-- por agente".
--
-- Princípio inviolável da SPEC: o caminho de ENVIO Z-API permanece byte-a-byte
-- intocado. Esta migração só altera o schema/dados da tabela `integrations`;
-- nenhum caminho de envio é tocado.
--
-- ============================================================================
-- ORDEM OBRIGATÓRIA (§2.2 — NÃO reordenar):
--   1. ALTER COLUMN instance_id DROP NOT NULL          (§2.2.1)
--   2. Dedup guard — DESATIVA (is_active=false) extras (§2.2.2)  ← NUNCA DELETE
--   3. CREATE UNIQUE INDEX CONCURRENTLY parcial em is_active=true (§2.2.3/§6.2)
--
-- DEPLOY: esta migração (com o dedup) DEVE estar aplicada ANTES de o route.ts
-- passar a fazer lookups por agente em produção (§8 passo 10) — senão duplicatas
-- pré-existentes podem produzir estado inconsistente.
--
-- ============================================================================
-- CONJUNTO CANÔNICO `WHATSAPP_PROVIDERS` (§2.2.2 / §2.3 — correção de validação #4)
-- A unicidade por agente cobre TODOS os providers WhatsApp aceitos pelo write
-- path — não só z-api/uazapi —, senão um alias legado (evolution/wppconnect/…)
-- escaparia da promessa "1 integração WhatsApp por agente".
--
--   ('z-api','uazapi','evolution','evolution-api','wppconnect',
--    'whatsapp','whatsapp-cloud','meta')
--
-- ⚠️ INVARIANTE DE SINCRONIA: este literal SQL (usado no dedup E no índice) DEVE
-- ser mantido idêntico à constante Python module-level
-- `app.services.integration_service.WHATSAPP_PROVIDERS` e à lista TS homônima do
-- `app/api/admin/integrations/route.ts`. Postgres/TS não importam a constante
-- Python — manter os três sincronizados é invariante documentado.
--
-- ============================================================================
-- POR QUE O DEDUP É OBRIGATÓRIO (§2.2 / §6.1):
-- Hoje NÃO há unicidade por `agent_id` (a única constraint é
-- `UNIQUE(provider, identifier)` — schema_completo.sql:1471-1472) e o GET
-- handler tolera múltiplas linhas por agente. Logo, produção pode conter
-- duplicatas. `CREATE UNIQUE INDEX` FALHA contra dados duplicados (23505 /
-- "duplicate key value") e `IF NOT EXISTS` NÃO ajuda (só pula se o índice já
-- existir, não se os dados violarem a unicidade). Portanto o dedup roda ANTES,
-- DESATIVANDO (is_active=false) as duplicatas — preserva histórico/auditoria — e
-- como o índice é PARCIAL em `is_active=true`, desativar É suficiente.
--
-- ============================================================================
-- INSPEÇÃO PRÉVIA (rode em STAGING antes de aplicar — dimensiona o impacto):
--   SELECT agent_id, count(*) FROM public.integrations
--     WHERE provider IN ('z-api','uazapi','evolution','evolution-api',
--                        'wppconnect','whatsapp','whatsapp-cloud','meta')
--       AND agent_id IS NOT NULL AND is_active = true
--     GROUP BY agent_id HAVING count(*) > 1;
--
-- ============================================================================
-- RE-EXECUÇÃO SEGURA (idempotência — §2.2.3):
--   - DROP NOT NULL é no-op se a coluna já for nullable.
--   - O dedup (UPDATE rn>1) é idempotente: numa 2ª rodada já há ≤1 linha ativa
--     por agente, então `rn > 1` não casa nada.
--   - O índice usa CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS.
--
-- ROLLBACK (documentado):
--   DROP INDEX CONCURRENTLY IF EXISTS public.uniq_whatsapp_active_integration_per_agent;
--   ALTER TABLE public.integrations ALTER COLUMN instance_id SET NOT NULL;  -- só se não houver linhas com instance_id NULL (uazapi grava NULL)
--   -- O dedup (is_active=false) NÃO é revertido automaticamente: as linhas
--   -- desativadas são histórico; reativar manualmente se necessário.
-- ============================================================================


-- ============================================================================
-- PARTE A — passos 1 e 2 (transacionais): DROP NOT NULL + dedup DESATIVA.
-- Estes dois passos são atômicos juntos: ou ambos aplicam, ou nenhum.
-- ============================================================================
BEGIN;

-- ---------------------------------------------------------------------------
-- Passo 1 (§2.2.1) — instance_id NOT NULL → nullable.
-- uazapi não tem instance_id; o frontend grava NULL para uazapi (§7.3). Não
-- afeta Z-API (continua sempre preenchendo). Menor superfície que gravar ''.
-- ---------------------------------------------------------------------------
ALTER TABLE public.integrations ALTER COLUMN instance_id DROP NOT NULL;

-- ---------------------------------------------------------------------------
-- Passo 2 (§2.2.2) — Dedup guard: DESATIVA (is_active=false), NUNCA DELETE.
-- Mantém ATIVA apenas a linha mais recente por agente; desativa as demais.
-- Determinismo: created_at DESC (default do schema, linha 757), com
-- updated_at DESC e ctid DESC como desempate estável. NULLS LAST garante que
-- linhas sem created_at não "ganhem" a posição de mais recente.
-- Registro de auditoria: RAISE NOTICE com a contagem de linhas desativadas.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    deactivated_count integer;
BEGIN
    WITH ranked AS (
        SELECT id,
               row_number() OVER (
                   PARTITION BY agent_id
                   ORDER BY created_at DESC NULLS LAST,
                            updated_at DESC NULLS LAST,
                            ctid DESC
               ) AS rn
        FROM public.integrations
        WHERE provider IN (
                  'z-api', 'uazapi', 'evolution', 'evolution-api',
                  'wppconnect', 'whatsapp', 'whatsapp-cloud', 'meta'
              )
          AND agent_id IS NOT NULL
          AND is_active = true
    )
    UPDATE public.integrations i
       SET is_active = false,
           updated_at = now()
      FROM ranked r
     WHERE i.id = r.id
       AND r.rn > 1;

    GET DIAGNOSTICS deactivated_count = ROW_COUNT;
    RAISE NOTICE '[uazapi migration] Dedup: % integração(ões) WhatsApp ativa(s) extra(s) DESATIVADA(s) (is_active=false, preservadas como histórico).', deactivated_count;
END $$;

COMMIT;


-- ============================================================================
-- PARTE B — passo 3 (§2.2.3 / §6.2): índice ÚNICO PARCIAL, CONCURRENTLY.
--
-- ⚠️ FORA de bloco transacional: CREATE INDEX CONCURRENTLY NÃO pode rodar dentro
-- de uma transação (BEGIN/COMMIT). Por isso este passo vem DEPOIS do COMMIT
-- acima e não está envolto em BEGIN/COMMIT.
--
-- Garante NO MÁXIMO UMA integração WhatsApp ATIVA por agente, em todo o
-- WHATSAPP_PROVIDERS. Linhas inativas (histórico) e globais (agent_id IS NULL)
-- NÃO são restringidas. CONCURRENTLY evita lock ACCESS EXCLUSIVE.
-- Idempotente via IF NOT EXISTS.
--
-- ⚠️ Se o runner de migração envolver cada arquivo num BEGIN/COMMIT implícito,
-- aplicar esta PARTE B separadamente (psql sem -1, ou janela de manutenção com
-- CREATE UNIQUE INDEX não-CONCURRENTLY) — sempre DEPOIS do dedup da PARTE A.
-- ============================================================================
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uniq_whatsapp_active_integration_per_agent
    ON public.integrations (agent_id)
    WHERE provider IN (
              'z-api', 'uazapi', 'evolution', 'evolution-api',
              'wppconnect', 'whatsapp', 'whatsapp-cloud', 'meta'
          )
      AND agent_id IS NOT NULL
      AND is_active = true;

COMMENT ON INDEX public.uniq_whatsapp_active_integration_per_agent IS
    'Exclusividade WhatsApp (SPEC-whatsapp-uazapi §6.2): no máximo UMA integração WhatsApp ATIVA por agente, sobre todo WHATSAPP_PROVIDERS. Parcial em is_active=true — linhas inativas são histórico e não colidem. Violação => 23505, mapeado para HTTP 409 pelo route.ts.';
