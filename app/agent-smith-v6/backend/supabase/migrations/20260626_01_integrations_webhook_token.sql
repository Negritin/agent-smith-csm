-- ============================================================================
-- Token de Webhook por Tenant (Fase 1) — FUNDAÇÃO: colunas + índice em
-- public.integrations para o token de webhook por-integração.
--
-- CONTEXTO DO PROJETO:
-- Os webhooks de WhatsApp (z-api / uazapi / evolution) eram autenticados por um
-- ÚNICO segredo global por provider (ZAPI_/UAZAPI_/EVOLUTION_WEBHOOK_SECRET) e o
-- tenant era escolhido a partir do `connectedPhone` do corpo da requisição —
-- isso quebra o isolamento multi-tenant (forja cross-tenant). A solução é um
-- token de webhook POR INTEGRAÇÃO, gerado server-side (256 bits), que é AO MESMO
-- TEMPO a credencial de autenticação E a chave de roteamento de tenant. A URL do
-- cliente passa a ser `.../api/v1/webhook/{provider}/{token}` e o tenant é
-- resolvido pelo token, não pelo `connectedPhone`.
--
-- Esta migração é a FUNDAÇÃO (Fase 1): só adiciona as 4 colunas aditivas e o
-- índice de lookup. NÃO mexe na borda (webhook.py continua com segredo global
-- até o cutover da Fase 2) e NÃO gera tokens — o backfill app-side (script
-- backend, formato `wh_{tag}_{secrets.token_urlsafe(32)}`) preenche os valores
-- ANTES do deploy token-only. As colunas toleram a janela NULL.
--
-- ----------------------------------------------------------------------------
-- IDEMPOTÊNCIA
-- ----------------------------------------------------------------------------
-- ADD COLUMN IF NOT EXISTS + CREATE INDEX IF NOT EXISTS: re-aplicar é no-op (não
-- erro). Não edita migrations já aplicadas (schema_completo.sql intacto). Seguro
-- re-rodar.
--
-- ============================================================================
-- ⚠️ POR QUE `CREATE INDEX` SIMPLES E NÃO `CONCURRENTLY`:
-- Mesmo padrão de 20260625_03_whatsapp_seam_unique_index.sql e
-- 20260621_90_concurrent_indexes.sql: CREATE INDEX não-concorrente roda em
-- QUALQUER runner, inclusive o Supabase SQL Editor — CONCURRENTLY falha com
-- 25001 ("cannot run inside a transaction block") porque o Editor envolve tudo
-- numa transação. A tabela public.integrations é PEQUENA (poucas linhas por
-- tenant), então o SHARE lock do build é instantâneo e seguro em produção.
--
-- ============================================================================
-- ⚠️ ÍNDICE PROVIDER-AGNÓSTICO (SEM `provider IN (...)`):
-- A unicidade NÃO é escopada por provider (decisão D7 da SPEC). Um token opaco
-- aleatório de 256 bits já é globalmente único — escopar por provider só
-- adicionaria uma 4ª ocorrência do literal canônico {z-api,uazapi,evolution}
-- (hoje sincronizado em integration_service.WHATSAPP_PROVIDERS, route.ts e na
-- seam migration 20260625_03), sem ganho. O `WHERE webhook_token_hash IS NOT
-- NULL` (índice parcial) evita colisão das linhas legadas NULL durante o rollout
-- (janela em que tokens ainda não foram backfilled).
--
-- ============================================================================
-- ORDEM DE DEPLOY (invariante):
--   1. Esta migração (colunas + índice).            <- VOCÊ ESTÁ AQUI
--   2. Backfill app-side (toda integração WhatsApp, ATIVAS e INATIVAS, ganha
--      token+hash). Gate de completude: count(*) WHERE webhook_token_hash IS
--      NULL AND provider IN (...) == 0.
--   3. UI exibindo as URLs novas (Fase 1, copiar DESABILITADO).
--   4. Cutover da borda para token-only + remoção dos 3 segredos (Fase 2).
-- A migração + backfill rodam ANTES de o backend token-only ir ao ar — senão
-- integrações existentes ficariam sem token para colocar na URL nova.
--
-- ROLLBACK (aditivo, sem consumidor antes do cutover da borda — impacto zero):
--   DROP INDEX IF EXISTS public.uniq_integrations_webhook_token_hash;
--   ALTER TABLE public.integrations DROP COLUMN IF EXISTS webhook_token_rotated_at;
--   ALTER TABLE public.integrations DROP COLUMN IF EXISTS webhook_token_prefix;
--   ALTER TABLE public.integrations DROP COLUMN IF EXISTS webhook_token_hash;
--   ALTER TABLE public.integrations DROP COLUMN IF EXISTS webhook_token;
-- ============================================================================

-- ----------------------------------------------------------------------------
-- (1) COLUNAS ADITIVAS (todas IF NOT EXISTS, todas NULL no rollout).
-- ----------------------------------------------------------------------------
ALTER TABLE public.integrations
    ADD COLUMN IF NOT EXISTS webhook_token text;

ALTER TABLE public.integrations
    ADD COLUMN IF NOT EXISTS webhook_token_hash text;

ALTER TABLE public.integrations
    ADD COLUMN IF NOT EXISTS webhook_token_prefix text;

ALTER TABLE public.integrations
    ADD COLUMN IF NOT EXISTS webhook_token_rotated_at timestamptz;

-- ----------------------------------------------------------------------------
-- (2) ÍNDICE de lookup do inbound: UNIQUE PARCIAL em webhook_token_hash.
--     Global (sem `provider IN`), parcial-on-not-null. Lookup O(1) por hash +
--     unicidade do token; tolera a janela NULL das linhas legadas/pré-backfill.
-- ----------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uniq_integrations_webhook_token_hash
    ON public.integrations (webhook_token_hash)
    WHERE webhook_token_hash IS NOT NULL;

COMMENT ON INDEX public.uniq_integrations_webhook_token_hash IS
    'Lookup O(1) + unicidade do token de webhook por-integração. Parcial em webhook_token_hash IS NOT NULL para não colidir as linhas legadas NULL durante o rollout (pré-backfill). Provider-agnóstico de propósito (D7): token opaco de 256 bits já é globalmente único, sem `provider IN` (evita 4ª ocorrência do literal canônico {z-api,uazapi,evolution}).';

-- ----------------------------------------------------------------------------
-- (3) DOCUMENTAÇÃO das colunas (semântica + invariante verify-only vs replayável).
-- ----------------------------------------------------------------------------
COMMENT ON COLUMN public.integrations.webhook_token IS
    'Token de webhook em TEXTO PURO, mantido apenas para RE-EXIBIÇÃO da URL no GET admin (o cliente re-copia a URL quando quiser). NUNCA é lido no caminho inbound (que casa por hash) e NUNCA vai para log/Sentry/audit. Verify-only (inbound) — NÃO confundir com `token`/`client_token`, que são credenciais de ENVIO (outbound) e precisam continuar replayáveis.';

COMMENT ON COLUMN public.integrations.webhook_token_hash IS
    'SHA-256 hex(64) do token completo. Credencial BEARER inbound por-integração = autenticação + chave de roteamento de tenant. Chave de lookup O(1) do inbound (índice UNIQUE parcial). A borda casa por este hash (hmac.compare_digest), nunca pelo `connectedPhone` do corpo — é o que fecha a forja cross-tenant.';

COMMENT ON COLUMN public.integrations.webhook_token_prefix IS
    'Primeiros 12 chars do token (ex. ''wh_zapi_aB3d''). NÃO-SECRETO — usado em UI/audit/log (observabilidade/grep) no lugar do token cru. Não revela entropia.';

COMMENT ON COLUMN public.integrations.webhook_token_rotated_at IS
    'Timestamp da última geração/rotação do token (endpoint de regeneração). NULL até o token ser gerado.';

-- ----------------------------------------------------------------------------
-- (4) DIAGNÓSTICO informativo (NÃO bloqueia): conta integrações WhatsApp ainda
--     sem token. Espera-se que o backfill app-side (próximo passo de deploy)
--     zere esta contagem antes do cutover token-only (gate de completude).
-- ----------------------------------------------------------------------------
DO $$
DECLARE
    pending_count integer;
BEGIN
    SELECT count(*)
      INTO pending_count
      FROM public.integrations
     WHERE provider IN ('z-api', 'uazapi', 'evolution')
       AND webhook_token_hash IS NULL;

    RAISE NOTICE '[webhook token 1/1] % integração(ões) WhatsApp sem token (webhook_token_hash IS NULL). Informativo — NÃO bloqueia. Rode o backfill app-side ANTES do cutover token-only (gate de completude: esta contagem deve chegar a 0).', pending_count;
END $$;
