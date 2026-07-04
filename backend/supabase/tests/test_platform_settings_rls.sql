-- Regression test for ALTO-002: RLS + REVOKE on public.platform_settings.
--
-- How to run (against a Postgres/Supabase database that already has the base
-- schema applied, i.e. the table from 20260530_platform_settings.sql exists and
-- the Supabase roles anon/authenticated/service_role are present):
--
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
--       -f backend/supabase/tests/test_platform_settings_rls.sql
--
-- The script (mirrors test_agents_updated_at_trigger.sql):
--   1. Applies the migration TWICE (proves idempotency — ENABLE RLS / REVOKE /
--      GRANT are all idempotent, so the 2nd pass must not error).
--   2. Asserts RLS is enabled on the table.
--   3. Asserts anon AND authenticated have ZERO privileges (SELECT/INSERT/
--      UPDATE/DELETE all denied) via has_table_privilege.
--   4. Asserts service_role keeps full access.
--   5. Runtime proof: SET ROLE anon / authenticated and confirm a SELECT raises
--      insufficient_privilege ("permission denied for table platform_settings").
--
-- Any failed assertion raises an exception, which (with ON_ERROR_STOP=1) makes
-- psql exit non-zero, so this file doubles as a CI gate.

\set ON_ERROR_STOP on

\echo '== Applying migration (1st pass) =='
\ir ../migrations/20260624_platform_settings_rls.sql

\echo '== Applying migration (2nd pass - proves idempotency) =='
\ir ../migrations/20260624_platform_settings_rls.sql

\echo '== Running RLS / grant assertions =='

DO $$
DECLARE
    v_priv text;
BEGIN
    --------------------------------------------------------------------
    -- (0) Supabase roles must exist for this gate to be meaningful.
    --------------------------------------------------------------------
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon')
       OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated')
       OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        RAISE EXCEPTION
            'FAIL: Supabase roles anon/authenticated/service_role missing — run against a Supabase DB';
    END IF;

    --------------------------------------------------------------------
    -- (1) RLS must be enabled on public.platform_settings.
    --------------------------------------------------------------------
    PERFORM 1
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relname = 'platform_settings'
      AND c.relrowsecurity IS TRUE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'FAIL: RLS is NOT enabled on public.platform_settings';
    END IF;
    RAISE NOTICE 'PASS: RLS enabled on public.platform_settings';

    --------------------------------------------------------------------
    -- (2) anon and authenticated must have NO privileges at all.
    --------------------------------------------------------------------
    FOREACH v_priv IN ARRAY ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE'] LOOP
        IF has_table_privilege('anon', 'public.platform_settings', v_priv) THEN
            RAISE EXCEPTION 'FAIL: anon still has % on platform_settings', v_priv;
        END IF;
        IF has_table_privilege('authenticated', 'public.platform_settings', v_priv) THEN
            RAISE EXCEPTION 'FAIL: authenticated still has % on platform_settings', v_priv;
        END IF;
    END LOOP;
    RAISE NOTICE 'PASS: anon and authenticated have zero privileges (SELECT/INSERT/UPDATE/DELETE)';

    --------------------------------------------------------------------
    -- (3) service_role must retain full access (GRANT ALL).
    --------------------------------------------------------------------
    FOREACH v_priv IN ARRAY ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE'] LOOP
        IF NOT has_table_privilege('service_role', 'public.platform_settings', v_priv) THEN
            RAISE EXCEPTION 'FAIL: service_role lost % on platform_settings', v_priv;
        END IF;
    END LOOP;
    RAISE NOTICE 'PASS: service_role retains full access';
END $$;

\echo '== Runtime proof: anon SELECT must be denied =='

DO $$
BEGIN
    BEGIN
        SET LOCAL ROLE anon;
        PERFORM 1 FROM public.platform_settings LIMIT 1;
        -- If we get here, the REVOKE failed.
        RAISE EXCEPTION 'FAIL: anon was able to SELECT from platform_settings';
    EXCEPTION
        WHEN insufficient_privilege THEN
            RAISE NOTICE 'PASS: anon SELECT denied (permission denied)';
    END;
    RESET ROLE;
END $$;

\echo '== Runtime proof: authenticated SELECT must be denied =='

DO $$
BEGIN
    BEGIN
        SET LOCAL ROLE authenticated;
        PERFORM 1 FROM public.platform_settings LIMIT 1;
        RAISE EXCEPTION 'FAIL: authenticated was able to SELECT from platform_settings';
    EXCEPTION
        WHEN insufficient_privilege THEN
            RAISE NOTICE 'PASS: authenticated SELECT denied (permission denied)';
    END;
    RESET ROLE;
END $$;

\echo '== ALL TESTS PASSED =='
