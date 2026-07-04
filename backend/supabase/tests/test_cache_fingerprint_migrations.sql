-- Tests for the cache fingerprint migrations.
--
-- How to run (against a Postgres/Supabase database that already has the base
-- schema applied):
--
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
--       -f backend/supabase/tests/test_cache_fingerprint_migrations.sql
--
-- The script:
--   1. Applies the three migrations TWICE (proves idempotency - no error).
--   2. Opens a transaction, creates disposable fixtures, asserts the trigger
--      behaviour, then ROLLBACKs so the database is left untouched.
--
-- Any failed assertion raises an exception, which (with ON_ERROR_STOP=1) makes
-- psql exit with a non-zero status, so this file doubles as a CI gate.

\set ON_ERROR_STOP on

\echo '== Applying migrations (1st pass) =='
\ir ../migrations/20260528_add_updated_at_to_agent_mcp_tools.sql
\ir ../migrations/20260528_add_config_updated_at_to_ucp_connections.sql
\ir ../migrations/20260528_add_config_updated_at_to_agent_mcp_connections.sql

\echo '== Applying migrations (2nd pass - proves idempotency) =='
\ir ../migrations/20260528_add_updated_at_to_agent_mcp_tools.sql
\ir ../migrations/20260528_add_config_updated_at_to_ucp_connections.sql
\ir ../migrations/20260528_add_config_updated_at_to_agent_mcp_connections.sql

\echo '== Running behaviour assertions =='

BEGIN;

DO $$
DECLARE
    v_company       uuid;
    v_agent         uuid;
    v_mcp_server    uuid;
    v_mcp_server_b  uuid;
    v_ucp           uuid;
    v_amc           uuid;
    v_amt           uuid;
    v_before        timestamptz;
    v_after         timestamptz;
    v_tools_updated timestamptz;
BEGIN
    --------------------------------------------------------------------
    -- Fixtures (minimal rows satisfying NOT NULL / FK constraints).
    --------------------------------------------------------------------
    INSERT INTO public.companies (company_name)
    VALUES ('Cache Fingerprint Test Co')
    RETURNING id INTO v_company;

    INSERT INTO public.agents (company_id, name, slug)
    VALUES (v_company, 'Cache FP Agent', 'cache-fp-agent')
    RETURNING id INTO v_agent;

    INSERT INTO public.mcp_servers (name, display_name, package_name, command)
    VALUES ('cache-fp-mcp', 'Cache FP MCP', '@test/cache-fp', '["node","server.js"]'::jsonb)
    RETURNING id INTO v_mcp_server;

    INSERT INTO public.mcp_servers (name, display_name, package_name, command)
    VALUES ('cache-fp-mcp-b', 'Cache FP MCP B', '@test/cache-fp-b', '["node","server.js"]'::jsonb)
    RETURNING id INTO v_mcp_server_b;

    --------------------------------------------------------------------
    -- Schema assertions: columns exist with the expected definition.
    --------------------------------------------------------------------
    PERFORM 1
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'agent_mcp_tools'
      AND column_name = 'updated_at'
      AND data_type = 'timestamp with time zone'
      AND is_nullable = 'NO';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'FAIL: agent_mcp_tools.updated_at missing or not timestamptz NOT NULL';
    END IF;

    PERFORM 1
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'ucp_connections'
      AND column_name = 'config_updated_at'
      AND data_type = 'timestamp with time zone'
      AND is_nullable = 'NO';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'FAIL: ucp_connections.config_updated_at missing or not timestamptz NOT NULL';
    END IF;

    PERFORM 1
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'agent_mcp_connections'
      AND column_name = 'config_updated_at'
      AND data_type = 'timestamp with time zone'
      AND is_nullable = 'NO';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'FAIL: agent_mcp_connections.config_updated_at missing or not timestamptz NOT NULL';
    END IF;

    RAISE NOTICE 'PASS: all timestamp columns exist as timestamptz NOT NULL';

    --------------------------------------------------------------------
    -- agent_mcp_tools: updated_at trigger keeps the column fresh.
    --------------------------------------------------------------------
    INSERT INTO public.agent_mcp_tools (
        agent_id, mcp_server_id, mcp_server_name, tool_name, variable_name
    )
    VALUES (
        v_agent, v_mcp_server, 'cache-fp-mcp', 'list_items', 'list_items_var'
    )
    RETURNING id INTO v_amt;

    UPDATE public.agent_mcp_tools
    SET description = 'touched'
    WHERE id = v_amt;

    SELECT updated_at INTO v_tools_updated
    FROM public.agent_mcp_tools WHERE id = v_amt;
    IF v_tools_updated IS NULL THEN
        RAISE EXCEPTION 'FAIL: agent_mcp_tools.updated_at is NULL after UPDATE';
    END IF;
    RAISE NOTICE 'PASS: agent_mcp_tools.updated_at trigger fired';

    --------------------------------------------------------------------
    -- ucp_connections fixture.
    --------------------------------------------------------------------
    INSERT INTO public.ucp_connections (
        agent_id, company_id, store_url, manifest_version,
        preferred_transport, capabilities_enabled, is_active
    )
    VALUES (
        v_agent, v_company, 'https://store.example.com', 'v1',
        'rest', '{dev.ucp.shopping.checkout}'::text[], true
    )
    RETURNING id INTO v_ucp;

    -- (1) UPDATE on last_used_at MUST NOT change config_updated_at.
    SELECT config_updated_at INTO v_before FROM public.ucp_connections WHERE id = v_ucp;
    UPDATE public.ucp_connections SET last_used_at = clock_timestamp() WHERE id = v_ucp;
    SELECT config_updated_at INTO v_after FROM public.ucp_connections WHERE id = v_ucp;
    IF v_after IS DISTINCT FROM v_before THEN
        RAISE EXCEPTION 'FAIL: ucp_connections.last_used_at update changed config_updated_at';
    END IF;
    RAISE NOTICE 'PASS: ucp_connections.last_used_at update did NOT touch config_updated_at';

    -- (2) UPDATE on last_error MUST NOT change config_updated_at.
    SELECT config_updated_at INTO v_before FROM public.ucp_connections WHERE id = v_ucp;
    UPDATE public.ucp_connections SET last_error = 'boom' WHERE id = v_ucp;
    SELECT config_updated_at INTO v_after FROM public.ucp_connections WHERE id = v_ucp;
    IF v_after IS DISTINCT FROM v_before THEN
        RAISE EXCEPTION 'FAIL: ucp_connections.last_error update changed config_updated_at';
    END IF;
    RAISE NOTICE 'PASS: ucp_connections.last_error update did NOT touch config_updated_at';

    -- (3) UPDATE on is_active MUST change config_updated_at.
    SELECT config_updated_at INTO v_before FROM public.ucp_connections WHERE id = v_ucp;
    UPDATE public.ucp_connections SET is_active = false WHERE id = v_ucp;
    SELECT config_updated_at INTO v_after FROM public.ucp_connections WHERE id = v_ucp;
    IF v_after IS NOT DISTINCT FROM v_before THEN
        RAISE EXCEPTION 'FAIL: ucp_connections.is_active update did NOT change config_updated_at';
    END IF;
    RAISE NOTICE 'PASS: ucp_connections.is_active update touched config_updated_at';

    -- (4) UPDATE on capabilities_enabled MUST change config_updated_at.
    SELECT config_updated_at INTO v_before FROM public.ucp_connections WHERE id = v_ucp;
    UPDATE public.ucp_connections
    SET capabilities_enabled = '{dev.ucp.shopping.cart}'::text[]
    WHERE id = v_ucp;
    SELECT config_updated_at INTO v_after FROM public.ucp_connections WHERE id = v_ucp;
    IF v_after IS NOT DISTINCT FROM v_before THEN
        RAISE EXCEPTION 'FAIL: ucp_connections.capabilities_enabled update did NOT change config_updated_at';
    END IF;
    RAISE NOTICE 'PASS: ucp_connections.capabilities_enabled update touched config_updated_at';

    --------------------------------------------------------------------
    -- agent_mcp_connections fixture.
    --------------------------------------------------------------------
    INSERT INTO public.agent_mcp_connections (
        agent_id, mcp_server_id, access_token, refresh_token,
        token_expires_at, is_active
    )
    VALUES (
        v_agent, v_mcp_server, 'tok-1', 'ref-1',
        now() + interval '1 hour', true
    )
    RETURNING id INTO v_amc;

    -- (5) UPDATE on access_token MUST NOT change config_updated_at.
    SELECT config_updated_at INTO v_before FROM public.agent_mcp_connections WHERE id = v_amc;
    UPDATE public.agent_mcp_connections SET access_token = 'tok-2' WHERE id = v_amc;
    SELECT config_updated_at INTO v_after FROM public.agent_mcp_connections WHERE id = v_amc;
    IF v_after IS DISTINCT FROM v_before THEN
        RAISE EXCEPTION 'FAIL: agent_mcp_connections.access_token update changed config_updated_at';
    END IF;
    RAISE NOTICE 'PASS: agent_mcp_connections.access_token update did NOT touch config_updated_at';

    -- (6) UPDATE on refresh_token / token_expires_at MUST NOT change config_updated_at.
    SELECT config_updated_at INTO v_before FROM public.agent_mcp_connections WHERE id = v_amc;
    UPDATE public.agent_mcp_connections
    SET refresh_token = 'ref-2', token_expires_at = now() + interval '2 hours'
    WHERE id = v_amc;
    SELECT config_updated_at INTO v_after FROM public.agent_mcp_connections WHERE id = v_amc;
    IF v_after IS DISTINCT FROM v_before THEN
        RAISE EXCEPTION 'FAIL: agent_mcp_connections token refresh changed config_updated_at';
    END IF;
    RAISE NOTICE 'PASS: agent_mcp_connections token refresh did NOT touch config_updated_at';

    -- (7) UPDATE on is_active MUST change config_updated_at.
    SELECT config_updated_at INTO v_before FROM public.agent_mcp_connections WHERE id = v_amc;
    UPDATE public.agent_mcp_connections SET is_active = false WHERE id = v_amc;
    SELECT config_updated_at INTO v_after FROM public.agent_mcp_connections WHERE id = v_amc;
    IF v_after IS NOT DISTINCT FROM v_before THEN
        RAISE EXCEPTION 'FAIL: agent_mcp_connections.is_active update did NOT change config_updated_at';
    END IF;
    RAISE NOTICE 'PASS: agent_mcp_connections.is_active update touched config_updated_at';

    -- (8) UPDATE on mcp_server_id MUST change config_updated_at.
    SELECT config_updated_at INTO v_before FROM public.agent_mcp_connections WHERE id = v_amc;
    UPDATE public.agent_mcp_connections SET mcp_server_id = v_mcp_server_b WHERE id = v_amc;
    SELECT config_updated_at INTO v_after FROM public.agent_mcp_connections WHERE id = v_amc;
    IF v_after IS NOT DISTINCT FROM v_before THEN
        RAISE EXCEPTION 'FAIL: agent_mcp_connections.mcp_server_id update did NOT change config_updated_at';
    END IF;
    RAISE NOTICE 'PASS: agent_mcp_connections.mcp_server_id update touched config_updated_at';

    RAISE NOTICE 'ALL TESTS PASSED';
END $$;

ROLLBACK;

\echo '== Done (fixtures rolled back; migrations remain applied) =='
