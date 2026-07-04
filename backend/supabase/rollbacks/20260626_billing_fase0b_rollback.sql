-- ROLLBACK da FASE 0B (Sprint S2) — billing idempotência + outbox + RPCs + revoke.
-- Ordem REVERSA da aplicação (04 → 03 → 02 → 01): drop das funções ANTES de dropar a
-- coluna idempotency_key que elas referenciam.
--
-- Pré-condição de rollback de CÓDIGO: ANTES de rodar isto, reverter o código para o
-- claim-then-debit-then-compensate (billing_tasks) e desligar BILLING_OUTBOX_ENABLED,
-- senão o código novo chamaria RPCs inexistentes.

BEGIN;

-- (04) NÃO re-conceder debit_company_balance a anon/authenticated: reabriria o buraco de
-- segurança que a migration 04 fechou (qualquer anon/authenticated key drenaria o saldo de
-- qualquer empresa). O estado "original" era inseguro de propósito; mantemos restrito a
-- service_role mesmo no rollback. (Se for ESTRITAMENTE necessário restaurar o estado
-- pré-migration, rode o GRANT manualmente — não recomendado.)

-- (03) drop das RPCs novas.
DROP FUNCTION IF EXISTS public.process_token_usage_outbox(int, int);
DROP FUNCTION IF EXISTS public.bill_usage_group(uuid[], uuid, uuid, text, numeric);

-- (02) drop do outbox.
DROP TABLE IF EXISTS public.token_usage_outbox;

-- (01) drop dos índices/colunas de idempotência. A coluna é NULLABLE → drop limpo.
DROP INDEX IF EXISTS public.uq_credit_transactions_idempotency_key;
ALTER TABLE public.credit_transactions DROP COLUMN IF EXISTS idempotency_key;
DROP INDEX IF EXISTS public.ux_token_usage_logs_idempotency_key;
ALTER TABLE public.token_usage_logs ALTER COLUMN idempotency_key DROP DEFAULT;
ALTER TABLE public.token_usage_logs DROP COLUMN IF EXISTS idempotency_key;

COMMIT;
