-- FASE 0B / Sprint S2 — chaves de idempotência de billing (token_usage_logs + credit_transactions).
--
-- Context (spec §3.3.1 / §3.3.3):
-- 1) A gravação de uso (usage_service.track_cost_sync) precisa ser idempotente para
--    o retry/replay do outbox NÃO duplicar a linha cobrável: coluna materializada
--    idempotency_key (run_id do LLM quando válido, senão uuid4) + UNIQUE INDEX →
--    .upsert(on_conflict="idempotency_key", ignore_duplicates=True).
-- 2) O débito atômico (bill_usage_group) usa um INSERT-gate em credit_transactions
--    por idempotency_key (belt-and-suspenders sobre a claim-por-log).
--
-- DEVE rodar ANTES das migrations das RPCs (20260626_03_*) — senão ON CONFLICT
-- (idempotency_key) falha com 42P10 (índice inexistente).
--
-- Ordem cast-safe (NB-4/NB-5): ADD COLUMN sem default → backfill com pg_input_is_valid
-- (um run_id legado inválido NÃO aborta a migration) → SET DEFAULT → UNIQUE INDEX.
-- Coluna fica NULLABLE (reversível); o default cobre inserts novos. SEM CONCURRENTLY
-- (a migration roda em transação; convenção do repo — ver 20260621_90_concurrent_indexes.sql).
--
-- Rollback (combinado da FASE 0B): backend/supabase/rollbacks/20260626_billing_fase0b_rollback.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- token_usage_logs.idempotency_key (uuid)
-- ---------------------------------------------------------------------------
ALTER TABLE public.token_usage_logs
    ADD COLUMN IF NOT EXISTS idempotency_key uuid;  -- SEM default (senão backfill no-opa)

-- Backfill com `id` (PK, sempre único). NÃO usar run_id aqui: o cost_callback grava o
-- MESMO run_id em múltiplas emissões (nós/sub-agents/retries do callback compartilhado)
-- → um run_id duplicado em qualquer linha abortaria a migration inteira (UNIQUE INDEX,
-- 1 BEGIN/COMMIT). A idempotência por run_id só importa DAQUI PRA FRENTE (track_cost_sync,
-- S3); linhas legadas já foram processadas, então `id` único basta.
UPDATE public.token_usage_logs
   SET idempotency_key = id
 WHERE idempotency_key IS NULL;

ALTER TABLE public.token_usage_logs
    ALTER COLUMN idempotency_key SET DEFAULT gen_random_uuid();  -- inserts novos

CREATE UNIQUE INDEX IF NOT EXISTS ux_token_usage_logs_idempotency_key
    ON public.token_usage_logs (idempotency_key);

-- ---------------------------------------------------------------------------
-- credit_transactions.idempotency_key (text) — gate do débito de consumo.
-- Índice ÚNICO PARCIAL (só linhas com chave) p/ não colidir com linhas legadas
-- nem com o índice de stripe (uq_credit_transactions_stripe_payment_id).
-- ---------------------------------------------------------------------------
ALTER TABLE public.credit_transactions
    ADD COLUMN IF NOT EXISTS idempotency_key text;

CREATE UNIQUE INDEX IF NOT EXISTS uq_credit_transactions_idempotency_key
    ON public.credit_transactions (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

COMMIT;
