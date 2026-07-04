-- Schema fiel mínimo para validar as migrations de billing FASE 0B no Postgres local.
\set ON_ERROR_STOP on

DROP SCHEMA IF EXISTS public CASCADE;
CREATE SCHEMA public;

-- Roles do Supabase (para REVOKE/GRANT + has_function_privilege).
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN CREATE ROLE anon NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN CREATE ROLE authenticated NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN CREATE ROLE service_role NOLOGIN; END IF;
END $$;

-- ---- token_usage_logs (sem idempotency_key; a migration 01 adiciona) ----
CREATE TABLE public.token_usage_logs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL,
    agent_id uuid,
    service_type text NOT NULL,
    model_name text NOT NULL,
    input_tokens int NOT NULL DEFAULT 0,
    output_tokens int NOT NULL DEFAULT 0,
    total_cost_usd numeric NOT NULL DEFAULT 0,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    billed boolean DEFAULT false,  -- NULLABLE como no schema real (testa o gate `billed IS NOT TRUE`)
    billed_at timestamptz,
    cache_creation_tokens int NOT NULL DEFAULT 0,
    cache_read_tokens int NOT NULL DEFAULT 0,
    cached_tokens int NOT NULL DEFAULT 0
);

-- ---- credit_transactions (balance_after NULLABLE — insert-gate-first preenche depois) ----
CREATE TABLE public.credit_transactions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL,
    agent_id uuid,
    type text NOT NULL,
    amount_brl numeric(10,4) NOT NULL,
    balance_after numeric(10,4),
    model_name text,
    tokens_input int,
    tokens_output int,
    description text,
    stripe_payment_id text,
    created_at timestamptz NOT NULL DEFAULT now()
);
-- índice parcial de stripe (existe no schema real; garante que o índice de idem não colide)
CREATE UNIQUE INDEX uq_credit_transactions_stripe_payment_id
    ON public.credit_transactions (stripe_payment_id) WHERE stripe_payment_id IS NOT NULL;

-- ---- company_credits ----
CREATE TABLE public.company_credits (
    company_id uuid PRIMARY KEY,
    balance_brl numeric(10,4) NOT NULL DEFAULT 0,
    alert_80_sent boolean NOT NULL DEFAULT false,
    alert_100_sent boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- ---- llm_pricing ----
CREATE TABLE public.llm_pricing (
    model_name text PRIMARY KEY,
    sell_multiplier numeric NOT NULL DEFAULT 2.68
);

-- ---- stub debit_company_balance (alvo do REVOKE da migration 04) ----
CREATE FUNCTION public.debit_company_balance(p_company_id uuid, p_amount numeric)
RETURNS numeric LANGUAGE sql AS $$ SELECT 0::numeric $$;
-- estado "original" inseguro que a migration 04 vai fechar:
GRANT ALL ON FUNCTION public.debit_company_balance(uuid, numeric) TO anon;
GRANT ALL ON FUNCTION public.debit_company_balance(uuid, numeric) TO authenticated;

-- seeds
INSERT INTO public.llm_pricing(model_name, sell_multiplier) VALUES ('gpt-test', 2.0);
INSERT INTO public.company_credits(company_id, balance_brl)
    VALUES ('11111111-1111-1111-1111-111111111111', 1000.0000);

SELECT 'schema OK' AS status;
