-- G3 (integridade de billing) — F14: idempotência de pagamento Stripe no nível do banco.
--
-- Context:
-- A idempotência de invoice/top-up era garantida APENAS por um SELECT em
-- credit_transactions.stripe_payment_id (billing_core.is_payment_processed)
-- seguido de add_credits/reset_credits — uma corrida TOCTOU. O Stripe entrega
-- invoice.paid/checkout.session.completed AT-LEAST-ONCE (e refaz a entrega no
-- 500 do handler), então duas entregas quase simultâneas do mesmo pagamento
-- passam ambas pelo SELECT antes de qualquer INSERT e creditam o saldo 2x.
--
-- Fix: garantir a idempotência no banco com um índice UNIQUE PARCIAL em
-- stripe_payment_id. O segundo INSERT com o mesmo stripe_payment_id não-nulo
-- levanta unique_violation (Postgres 23505), que billing_core trata como
-- "já processado" (no-op idempotente, sem reaplicar o crédito).
--
-- PARCIAL (WHERE stripe_payment_id IS NOT NULL) porque débitos de consumo
-- gravam stripe_payment_id = NULL (billing_core.debit_credits não inclui o
-- campo); múltiplos NULL devem continuar permitidos e nunca colidir.
--
-- Idempotency:
-- CREATE UNIQUE INDEX IF NOT EXISTS garante que re-rodar a migração não levanta.
--
-- PRÉ-REQUISITO OPERACIONAL (rodar ANTES de aplicar — a criação do índice
-- FALHA se já existirem duplicatas):
--   SELECT stripe_payment_id, count(*)
--     FROM public.credit_transactions
--    WHERE stripe_payment_id IS NOT NULL
--    GROUP BY stripe_payment_id
--   HAVING count(*) > 1;
-- Se retornar linhas, deduplicar manualmente (manter a transação mais antiga,
-- apagar as repetições) antes de aplicar esta migração.
--
-- Rollback (documentado):
--   DROP INDEX IF EXISTS public.uq_credit_transactions_stripe_payment_id;

BEGIN;

CREATE UNIQUE INDEX IF NOT EXISTS uq_credit_transactions_stripe_payment_id
    ON public.credit_transactions (stripe_payment_id)
    WHERE stripe_payment_id IS NOT NULL;

COMMENT ON INDEX public.uq_credit_transactions_stripe_payment_id IS
    'Idempotência Stripe (F14): impede crédito duplicado sob entrega at-least-once. Parcial — débitos de consumo (stripe_payment_id NULL) não colidem.';

COMMIT;
