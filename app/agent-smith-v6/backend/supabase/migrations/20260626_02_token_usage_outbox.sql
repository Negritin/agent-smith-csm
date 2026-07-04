-- FASE 0B / Sprint S2 — token_usage_outbox: durabilidade da gravação de uso (nunca perder).
--
-- Context (spec §3.3.2):
-- usage_service.track_cost_sync grava o uso em token_usage_logs. Sob Broken pipe, o
-- INSERT pode falhar mesmo após retry. Hoje o código faz `except → return False` →
-- o log é DROPADO silenciosamente = vazamento de cobrança. Esta tabela é o outbox
-- durável: em falha do upsert primário, o registro é enfileirado aqui e um drenador
-- (RPC process_token_usage_outbox, migration 20260626_03) o reaplica idempotentemente.
--
-- dead-letter: attempts/max_attempts/dead_at — um payload permanentemente inválido
-- vira dead_at após max_attempts em vez de re-claimar pra sempre (loop infinito).
--
-- Segurança (convenção do repo — ver 20260621_99_rls_attendance_tables.sql):
-- RLS habilitada; REVOKE de anon/authenticated; GRANT a service_role (backend/worker).
--
-- Rollback (combinado da FASE 0B): backend/supabase/rollbacks/20260626_billing_fase0b_rollback.sql

BEGIN;

CREATE TABLE IF NOT EXISTS public.token_usage_outbox (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key uuid NOT NULL,
    company_id     uuid,
    -- log_entry COMPLETO de usage_service (company_id, agent_id, service_type [NOT NULL],
    -- model_name [NOT NULL], input/output_tokens, total_cost_usd, details, created_at,
    -- cache_*, idempotency_key). O drenador reconstrói a linha de token_usage_logs a partir daqui.
    payload        jsonb NOT NULL,
    attempts       int NOT NULL DEFAULT 0,
    max_attempts   int NOT NULL DEFAULT 10,
    dead_at        timestamptz,
    last_error     text,
    claimed_at     timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- Índice do claim do drenador: filtra dead_at IS NULL, ordena por created_at, reclaim por claimed_at.
CREATE INDEX IF NOT EXISTS ix_token_usage_outbox_claim
    ON public.token_usage_outbox (dead_at, claimed_at, created_at);

ALTER TABLE public.token_usage_outbox ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.token_usage_outbox FROM anon, authenticated;
GRANT ALL ON public.token_usage_outbox TO service_role;

COMMIT;
