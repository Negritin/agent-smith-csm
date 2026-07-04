-- Tests for the agents.updated_at BEFORE UPDATE trigger migration (F11, G2-R9).
--
-- How to run (against a Postgres/Supabase database that already has the base
-- schema applied):
--
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
--       -f backend/supabase/tests/test_agents_updated_at_trigger.sql
--
-- The script (mirrors test_cache_fingerprint_migrations.sql):
--   1. Applies the migration TWICE (proves idempotency - no error thanks to
--      DROP TRIGGER IF EXISTS).
--   2. Opens a transaction, creates a disposable agents fixture, asserts the
--      trigger is registered AND that a config UPDATE makes updated_at advance
--      (without the app setting the column), then ROLLBACKs.
--
-- NOTE on time semantics: public.update_updated_at_column() uses now(), which is
-- the TRANSACTION start time (constant within a txn). So we cannot prove the
-- bump by comparing two updates inside one transaction. Instead we prove the
-- BEFORE UPDATE trigger actually fires by writing a sentinel epoch value in the
-- UPDATE and asserting the trigger OVERRODE it to now() (i.e. updated_at moved
-- forward from the sentinel). This is transaction-safe and directly proves both
-- "the trigger fires" and "updated_at advances without the app setting it".
--
-- Any failed assertion raises an exception, which (with ON_ERROR_STOP=1) makes
-- psql exit non-zero, so this file doubles as a CI gate.

\set ON_ERROR_STOP on

\echo '== Applying migration (1st pass) =='
\ir ../migrations/20260601_add_updated_at_trigger_to_agents.sql

\echo '== Applying migration (2nd pass - proves idempotency) =='
\ir ../migrations/20260601_add_updated_at_trigger_to_agents.sql

\echo '== Running behaviour assertions =='

BEGIN;

DO $$
DECLARE
    v_company  uuid;
    v_agent    uuid;
    v_before   timestamptz;
    v_after    timestamptz;
    v_sentinel constant timestamptz := timestamptz 'epoch';
BEGIN
    --------------------------------------------------------------------
    -- Trigger is registered on public.agents.
    --------------------------------------------------------------------
    PERFORM 1
    FROM pg_trigger t
    JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relname = 'agents'
      AND t.tgname = 'update_agents_updated_at'
      AND NOT t.tgisinternal;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'FAIL: trigger update_agents_updated_at not present on public.agents';
    END IF;
    RAISE NOTICE 'PASS: trigger update_agents_updated_at is registered';

    --------------------------------------------------------------------
    -- Fixtures (minimal rows satisfying NOT NULL / FK constraints).
    --------------------------------------------------------------------
    INSERT INTO public.companies (company_name)
    VALUES ('Agents updated_at Trigger Test Co')
    RETURNING id INTO v_company;

    INSERT INTO public.agents (company_id, name, slug)
    VALUES (v_company, 'updated_at Trigger Agent', 'updated-at-trigger-agent')
    RETURNING id INTO v_agent;

    --------------------------------------------------------------------
    -- A config UPDATE must make updated_at advance, even when the app tries to
    -- write a stale sentinel: the BEFORE UPDATE trigger overrides NEW.updated_at
    -- back to now(). Proves the trigger fires and updated_at moves forward.
    --------------------------------------------------------------------
    UPDATE public.agents
    SET name = 'updated_at Trigger Agent (touched)',
        updated_at = v_sentinel
    WHERE id = v_agent;

    SELECT updated_at INTO v_after FROM public.agents WHERE id = v_agent;
    IF v_after IS NULL THEN
        RAISE EXCEPTION 'FAIL: agents.updated_at is NULL after UPDATE';
    END IF;
    IF v_after <= v_sentinel THEN
        RAISE EXCEPTION
            'FAIL: agents.updated_at trigger did NOT fire (still % <= sentinel %)',
            v_after, v_sentinel;
    END IF;
    RAISE NOTICE 'PASS: agents.updated_at trigger fired and advanced (%)' , v_after;

    RAISE NOTICE 'ALL TESTS PASSED';
END $$;

ROLLBACK;

\echo '== Done (fixtures rolled back; migration remains applied) =='
