-- CRITICO-001 (integridade de saldo) — RPCs atômicas de crédito/reset de saldo.
--
-- Context:
-- billing_core.add_credits/reset_credits aplicavam o saldo via read-modify-write
-- não-atômico em Python: SELECT balance_brl -> calcula new_balance -> upsert
-- balance_brl = new_balance. Sob duas concessões concorrentes (ex.: top-up +
-- renovação, ou débito de consumo simultâneo) o padrão perde updates (lost
-- update): ambas leem o mesmo saldo e a última gravação sobrescreve a outra.
--
-- Além disso, o gate de idempotência (INSERT-first em credit_transactions por
-- stripe_payment_id) ficava numa transação SEPARADA do upsert de saldo,
-- deixando janela para registro órfão (transação gravada mas saldo não aplicado,
-- ou vice-versa).
--
-- Fix: mover crédito e reset para statements atômicos no banco, espelhando
-- public.debit_company_balance. O INSERT-gate de idempotência e o UPDATE de
-- saldo ocorrem na MESMA função/transação:
--   1. INSERT em credit_transactions com ON CONFLICT (stripe_payment_id)
--      DO NOTHING (infere o índice parcial uq_credit_transactions_stripe_payment_id).
--      Duplicata (entrega at-least-once do Stripe) => no-op idempotente: retorna
--      o saldo atual SEM alterá-lo.
--   2. Caso contrário, aplica o saldo num único statement:
--        credit: balance_brl = balance_brl + p_amount  (incremento atômico)
--        reset:  balance_brl = p_amount                 (substituição)
--      garantindo a linha via INSERT ... ON CONFLICT (company_id) (constraint
--      tenant_credits_tenant_id_key).
--   3. Preenche balance_after da transação com o saldo final.
--
-- Segurança (espelha debit_company_balance + convenção do projeto):
-- SECURITY DEFINER, search_path fixo, REVOKE de anon/authenticated/PUBLIC e
-- GRANT EXECUTE só a service_role (backend/worker rodam sob service_role).
--
-- Idempotência da migração: CREATE OR REPLACE FUNCTION + grants idempotentes.
--
-- Rollback (documentado):
--   DROP FUNCTION IF EXISTS public.credit_company_balance(uuid, numeric, text, text, text);
--   DROP FUNCTION IF EXISTS public.reset_company_balance(uuid, numeric, text, text);
--   DROP INDEX IF EXISTS public.uq_subscriptions_one_active_per_company;

BEGIN;

-- ---------------------------------------------------------------------------
-- credit_company_balance: incremento atômico + idempotência Stripe.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.credit_company_balance(
    p_company_id uuid,
    p_amount numeric,
    p_stripe_payment_id text DEFAULT NULL,
    p_type text DEFAULT 'topup',
    p_description text DEFAULT NULL
) RETURNS numeric
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = public
    AS $$
DECLARE
    v_transaction_id uuid;
    v_new_balance NUMERIC;
BEGIN
    -- (1) Gate de idempotência: INSERT-first. Para p_stripe_payment_id não-nulo,
    -- uma duplicata colide com uq_credit_transactions_stripe_payment_id e o
    -- ON CONFLICT DO NOTHING não insere (v_transaction_id permanece NULL).
    -- p_stripe_payment_id NULL (bônus/ajuste) nunca colide (índice parcial).
    INSERT INTO public.credit_transactions (
        company_id, type, amount_brl, description, stripe_payment_id
    )
    VALUES (
        p_company_id, p_type, p_amount, p_description, p_stripe_payment_id
    )
    ON CONFLICT (stripe_payment_id) WHERE stripe_payment_id IS NOT NULL
    DO NOTHING
    RETURNING id INTO v_transaction_id;

    -- Duplicata: no-op idempotente. Retorna o saldo atual sem alterá-lo.
    IF v_transaction_id IS NULL THEN
        SELECT balance_brl INTO v_new_balance
        FROM public.company_credits
        WHERE company_id = p_company_id;
        RETURN COALESCE(v_new_balance, 0);
    END IF;

    -- (2) Incremento atômico do saldo, garantindo a linha.
    INSERT INTO public.company_credits (
        company_id, balance_brl, alert_80_sent, alert_100_sent, updated_at
    )
    VALUES (p_company_id, p_amount, false, false, NOW())
    ON CONFLICT (company_id) DO UPDATE
        SET balance_brl = public.company_credits.balance_brl + p_amount,
            alert_80_sent = false,
            alert_100_sent = false,
            updated_at = NOW()
    RETURNING balance_brl INTO v_new_balance;

    -- (3) Preenche balance_after agora que o saldo final é conhecido.
    UPDATE public.credit_transactions
    SET balance_after = v_new_balance
    WHERE id = v_transaction_id;

    RETURN v_new_balance;
END;
$$;

-- ---------------------------------------------------------------------------
-- reset_company_balance: substituição atômica (renovação) + idempotência.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.reset_company_balance(
    p_company_id uuid,
    p_amount numeric,
    p_stripe_payment_id text DEFAULT NULL,
    p_description text DEFAULT NULL
) RETURNS numeric
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = public
    AS $$
DECLARE
    v_transaction_id uuid;
    v_new_balance NUMERIC;
BEGIN
    -- (1) Gate de idempotência (mesma semântica do credit): uma re-entrega da
    -- mesma renovação (mesmo stripe_payment_id) colide e vira no-op.
    INSERT INTO public.credit_transactions (
        company_id, type, amount_brl, description, stripe_payment_id
    )
    VALUES (
        p_company_id, 'subscription', p_amount, p_description, p_stripe_payment_id
    )
    ON CONFLICT (stripe_payment_id) WHERE stripe_payment_id IS NOT NULL
    DO NOTHING
    RETURNING id INTO v_transaction_id;

    IF v_transaction_id IS NULL THEN
        SELECT balance_brl INTO v_new_balance
        FROM public.company_credits
        WHERE company_id = p_company_id;
        RETURN COALESCE(v_new_balance, 0);
    END IF;

    -- (2) RESET: saldo = p_amount (não acumula), garantindo a linha.
    INSERT INTO public.company_credits (
        company_id, balance_brl, alert_80_sent, alert_100_sent, updated_at
    )
    VALUES (p_company_id, p_amount, false, false, NOW())
    ON CONFLICT (company_id) DO UPDATE
        SET balance_brl = p_amount,
            alert_80_sent = false,
            alert_100_sent = false,
            updated_at = NOW()
    RETURNING balance_brl INTO v_new_balance;

    -- (3) Preenche balance_after.
    UPDATE public.credit_transactions
    SET balance_after = v_new_balance
    WHERE id = v_transaction_id;

    RETURN v_new_balance;
END;
$$;

-- ---------------------------------------------------------------------------
-- Grants: as RPCs rodam sob service_role (backend/worker). anon/authenticated
-- não recebem execução (convenção do projeto: SECURITY DEFINER + REVOKE +
-- GRANT service_role). Espelha o tratamento de debit_company_balance.
-- ---------------------------------------------------------------------------
REVOKE ALL ON FUNCTION public.credit_company_balance(uuid, numeric, text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.credit_company_balance(uuid, numeric, text, text, text) FROM anon;
REVOKE ALL ON FUNCTION public.credit_company_balance(uuid, numeric, text, text, text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.credit_company_balance(uuid, numeric, text, text, text) TO service_role;

REVOKE ALL ON FUNCTION public.reset_company_balance(uuid, numeric, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.reset_company_balance(uuid, numeric, text, text) FROM anon;
REVOKE ALL ON FUNCTION public.reset_company_balance(uuid, numeric, text, text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.reset_company_balance(uuid, numeric, text, text) TO service_role;

COMMENT ON FUNCTION public.credit_company_balance(uuid, numeric, text, text, text) IS
    'CRITICO-001: crédito atômico (balance += amount) com gate de idempotência Stripe (INSERT-first em credit_transactions) na MESMA transação. Substitui o read-modify-write de billing_core.add_credits.';
COMMENT ON FUNCTION public.reset_company_balance(uuid, numeric, text, text) IS
    'CRITICO-001: reset atômico de saldo (balance = amount) com gate de idempotência na MESMA transação. Substitui o read-modify-write de billing_core.reset_credits.';

COMMIT;

-- ---------------------------------------------------------------------------
-- MEDIO-009 (recomendado): unicidade de subscription ativa por empresa.
--
-- Reforça a seleção determinística de is_subscription_blocked impedindo > 1
-- subscription com status='active' por empresa. Em transação separada e guardado
-- por um pré-check para NÃO abortar a criação das RPCs caso já existam
-- duplicatas (deduplicar manualmente e re-rodar para criar o índice).
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM public.subscriptions
        WHERE status = 'active'
        GROUP BY company_id
        HAVING count(*) > 1
    ) THEN
        RAISE WARNING '[atomic_balance_rpcs] Existem subscriptions ativas duplicadas por empresa; índice uq_subscriptions_one_active_per_company NÃO criado. Deduplique e re-rode a migração.';
    ELSE
        CREATE UNIQUE INDEX IF NOT EXISTS uq_subscriptions_one_active_per_company
            ON public.subscriptions (company_id)
            WHERE status = 'active';
    END IF;
END $$;
