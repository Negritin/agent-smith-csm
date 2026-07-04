-- Dumped from database version 17.6
-- Dumped by pg_dump version 18.4 (Ubuntu 18.4-0ubuntu0.26.04.1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: private; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA IF NOT EXISTS private;


--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA IF NOT EXISTS public;


--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA public IS 'standard public schema';


--
-- Name: _attendance_create_sla(uuid, uuid, uuid, text, timestamp with time zone, timestamp with time zone, timestamp with time zone, jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public._attendance_create_sla(p_attendance_session_id uuid, p_conversation_id uuid, p_company_id uuid, p_sla_level text, p_started_at timestamp with time zone, p_first_response_deadline timestamp with time zone, p_resolution_deadline timestamp with time zone, p_policy_snapshot jsonb) RETURNS uuid
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_id uuid;
    v_policy_id uuid;
BEGIN
    v_policy_id := NULLIF(p_policy_snapshot->>'id', '')::uuid;

    INSERT INTO public.attendance_sla (
        attendance_session_id, conversation_id, company_id, policy_id, sla_level,
        started_at, first_response_deadline, resolution_deadline, policy_snapshot
    )
    VALUES (
        p_attendance_session_id, p_conversation_id, p_company_id, v_policy_id, p_sla_level,
        p_started_at, p_first_response_deadline, p_resolution_deadline, p_policy_snapshot
    )
    ON CONFLICT (attendance_session_id) DO NOTHING
    RETURNING id INTO v_id;

    IF v_id IS NULL THEN
        SELECT id INTO v_id
        FROM public.attendance_sla
        WHERE attendance_session_id = p_attendance_session_id;
        RETURN v_id;
    END IF;

    INSERT INTO public.sla_events (
        attendance_sla_id, attendance_session_id, conversation_id, company_id,
        event_type, actor_type
    )
    VALUES (
        v_id, p_attendance_session_id, p_conversation_id, p_company_id,
        'sla_started', 'system'
    );

    RETURN v_id;
END;
$$;


--
-- Name: _attendance_enqueue_handoff_notifications(uuid, uuid, uuid, uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public._attendance_enqueue_handoff_notifications(p_conversation_id uuid, p_attendance_session_id uuid, p_company_id uuid, p_agent_id uuid) RETURNS void
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF p_attendance_session_id IS NULL THEN
        RETURN;
    END IF;

    INSERT INTO public.notification_deliveries (
        company_id, conversation_id, attendance_session_id, recipient_id,
        event_type, idempotency_key, channel, recipient_value, status, next_attempt_at
    )
    SELECT
        p_company_id,
        p_conversation_id,
        p_attendance_session_id,
        d.id,
        'handoff_requested',
        p_attendance_session_id::text || ':handoff_requested:' || d.id::text,
        d.channel,
        d.recipient_value,
        'pending',
        now()
    FROM (
        SELECT DISTINCT ON (r.channel, r.recipient_normalized)
            r.id, r.channel, r.recipient_value
        FROM public.handoff_notification_recipients r
        WHERE r.company_id = p_company_id
          AND r.enabled = true
          AND (r.agent_id = p_agent_id OR r.agent_id IS NULL)
        -- Preferir a linha específica do agente: (agent_id IS NULL) = false ordena
        -- primeiro, então DISTINCT ON mantém a linha do agente quando há ambas.
        ORDER BY r.channel, r.recipient_normalized, (r.agent_id IS NULL) ASC
    ) AS d
    ON CONFLICT (idempotency_key) DO NOTHING;
END;
$$;


--
-- Name: _attendance_ensure_open_session(uuid, uuid, uuid, text, jsonb, text, uuid, uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public._attendance_ensure_open_session(p_conversation_id uuid, p_company_id uuid, p_agent_id uuid, p_status text, p_payload jsonb, p_actor_type text, p_actor_agent_id uuid, p_actor_user_id uuid) RETURNS uuid
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_id uuid;
BEGIN
    -- Já existe sessão viva? (parcial: open/human_requested/human_active/pending_customer)
    SELECT id INTO v_id
    FROM public.attendance_sessions
    WHERE conversation_id = p_conversation_id
      AND status IN ('open', 'human_requested', 'human_active', 'pending_customer')
    LIMIT 1;

    IF v_id IS NOT NULL THEN
        RETURN v_id;
    END IF;

    INSERT INTO public.attendance_sessions (
        conversation_id, company_id, agent_id, status,
        human_requested_by_type, human_requested_by_agent_id, human_requested_by_user_id,
        metadata
    )
    VALUES (
        p_conversation_id, p_company_id, p_agent_id, p_status,
        CASE WHEN p_status = 'human_requested' THEN coalesce(p_actor_type, 'agent') END,
        p_actor_agent_id, p_actor_user_id,
        coalesce(p_payload, '{}'::jsonb)
    )
    ON CONFLICT (conversation_id)
        WHERE status IN ('open', 'human_requested', 'human_active', 'pending_customer')
    DO NOTHING
    RETURNING id INTO v_id;

    IF v_id IS NULL THEN
        -- Corrida concorrente criou a sessão: re-leitura.
        SELECT id INTO v_id
        FROM public.attendance_sessions
        WHERE conversation_id = p_conversation_id
          AND status IN ('open', 'human_requested', 'human_active', 'pending_customer')
        LIMIT 1;
    END IF;

    RETURN v_id;
END;
$$;


--
-- Name: _attendance_record_event(uuid, uuid, uuid, uuid, text, text, uuid, uuid, jsonb, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public._attendance_record_event(p_conversation_id uuid, p_attendance_session_id uuid, p_company_id uuid, p_agent_id uuid, p_event_type text, p_actor_type text, p_actor_user_id uuid, p_actor_agent_id uuid, p_metadata jsonb, p_idempotency_key text) RETURNS uuid
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO public.conversation_events (
        conversation_id, attendance_session_id, company_id, agent_id,
        event_type, actor_type, actor_user_id, actor_agent_id, metadata, idempotency_key
    )
    VALUES (
        p_conversation_id, p_attendance_session_id, p_company_id, p_agent_id,
        p_event_type, p_actor_type, p_actor_user_id, p_actor_agent_id,
        coalesce(p_metadata, '{}'::jsonb), p_idempotency_key
    )
    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
    DO NOTHING
    RETURNING id INTO v_id;

    IF v_id IS NULL AND p_idempotency_key IS NOT NULL THEN
        SELECT id INTO v_id
        FROM public.conversation_events
        WHERE idempotency_key = p_idempotency_key;
    END IF;

    RETURN v_id;
END;
$$;


--
-- Name: agent_mcp_connections_touch_config_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.agent_mcp_connections_touch_config_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public'
    AS $$
BEGIN
    IF (
        OLD.is_active IS DISTINCT FROM NEW.is_active
        OR OLD.mcp_server_id IS DISTINCT FROM NEW.mcp_server_id
        OR OLD.connection_config IS DISTINCT FROM NEW.connection_config
    ) THEN
        NEW.config_updated_at = clock_timestamp();
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: bill_usage_group(uuid[], uuid, uuid, text, numeric); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.bill_usage_group(p_log_ids uuid[], p_company_id uuid, p_agent_id uuid, p_model_name text, p_dollar_rate numeric) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
DECLARE
    v_ids uuid[];
    v_cost_usd numeric;
    v_in int;
    v_out int;
    v_mult numeric;
    v_amount numeric;
    v_bal numeric;
    v_idem text;
    v_txn_id uuid;
BEGIN
    -- (1) CLAIM-POR-LOG = gate. `billed IS NOT TRUE` inclui linhas legadas billed=NULL.
    WITH claimed AS (
        UPDATE public.token_usage_logs
           SET billed = true, billed_at = now()
         WHERE id = ANY(p_log_ids) AND billed IS NOT TRUE
        RETURNING id, total_cost_usd, input_tokens, output_tokens
    )
    SELECT array_agg(id ORDER BY id),
           COALESCE(sum(total_cost_usd), 0),
           COALESCE(sum(input_tokens), 0),
           COALESCE(sum(output_tokens), 0)
      INTO v_ids, v_cost_usd, v_in, v_out
      FROM claimed;

    IF v_ids IS NULL THEN
        RETURN;
    END IF;

    -- (2) amount = custo_usd das REIVINDICADAS × dollar_rate × sell_multiplier(modelo).
    SELECT COALESCE(sell_multiplier, 2.68) INTO v_mult
      FROM public.llm_pricing WHERE model_name = p_model_name;
    v_amount := round(v_cost_usd * p_dollar_rate * COALESCE(v_mult, 2.68), 4);

    -- Grupo sem custo: logs ficam billed=true, sem débito nem ledger.
    IF v_amount = 0 THEN
        RETURN;
    END IF;

    v_idem := md5(array_to_string(v_ids, ','));

    -- (3) GATE no ledger PRIMEIRO (insert-first idempotente).
    INSERT INTO public.credit_transactions (
        company_id, agent_id, type, amount_brl, model_name,
        tokens_input, tokens_output, idempotency_key
    ) VALUES (
        p_company_id, p_agent_id, 'consumption', -v_amount, p_model_name,
        v_in, v_out, v_idem
    )
    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
    DO NOTHING
    RETURNING id INTO v_txn_id;

    IF v_txn_id IS NULL THEN
        RETURN;  -- grupo já cobrado: sem 2º débito.
    END IF;

    -- (4) débito atômico do saldo; captura balance_after.
    INSERT INTO public.company_credits (company_id, balance_brl, updated_at)
         VALUES (p_company_id, -v_amount, now())
    ON CONFLICT (company_id) DO UPDATE
         SET balance_brl = public.company_credits.balance_brl - v_amount,
             updated_at = now()
    RETURNING balance_brl INTO v_bal;

    -- (5) preenche balance_after.
    UPDATE public.credit_transactions SET balance_after = v_bal WHERE id = v_txn_id;
END;
$$;


--
-- Name: check_and_increment_rate_limit(text, uuid, integer, integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.check_and_increment_rate_limit(p_identifier text, p_agent_id uuid, p_max_requests integer DEFAULT 50, p_window_minutes integer DEFAULT 60) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    AS $$
DECLARE
    v_record RECORD;
    v_new_count INTEGER;
    v_window_seconds INTEGER;
BEGIN
    v_window_seconds := p_window_minutes * 60;

    SELECT id, request_count, window_start
    INTO v_record
    FROM widget_rate_limits
    WHERE identifier = p_identifier AND agent_id = p_agent_id
    FOR UPDATE;

    IF FOUND THEN
        IF EXTRACT(EPOCH FROM (NOW() - v_record.window_start)) > v_window_seconds THEN
            UPDATE widget_rate_limits
            SET request_count = 1, window_start = NOW()
            WHERE id = v_record.id;
            RETURN 1;
        END IF;

        IF v_record.request_count >= p_max_requests THEN
            RETURN -1;
        END IF;

        UPDATE widget_rate_limits
        SET request_count = request_count + 1
        WHERE id = v_record.id
        RETURNING request_count INTO v_new_count;

        RETURN v_new_count;
    ELSE
        INSERT INTO widget_rate_limits (identifier, identifier_type, agent_id, request_count, window_start)
        VALUES (p_identifier, 'session', p_agent_id, 1, NOW())
        ON CONFLICT (identifier, agent_id, identifier_type) DO UPDATE
        SET request_count = widget_rate_limits.request_count + 1
        RETURNING request_count INTO v_new_count;

        RETURN COALESCE(v_new_count, 1);
    END IF;
END;
$$;


--
-- Name: create_user_account(character varying, character varying, character varying, character varying, character varying, character varying, date, uuid, character varying, character varying, boolean); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.create_user_account(p_first_name character varying, p_last_name character varying, p_email character varying, p_password_hash character varying, p_cpf character varying, p_phone character varying, p_birth_date date, p_company_id uuid DEFAULT NULL::uuid, p_status character varying DEFAULT 'pending'::character varying, p_role character varying DEFAULT 'member'::character varying, p_is_owner boolean DEFAULT false) RETURNS TABLE(id uuid, email character varying, first_name character varying, last_name character varying, company_id uuid, role character varying, status character varying, is_owner boolean, created_at timestamp without time zone)
    LANGUAGE plpgsql SECURITY DEFINER
    AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM users_v2 WHERE users_v2.email = p_email) THEN
        RAISE EXCEPTION 'Email already exists';
    END IF;

    IF p_role NOT IN ('admin_company', 'member') THEN
        RAISE EXCEPTION 'Invalid role';
    END IF;

    IF p_status NOT IN ('active', 'pending', 'suspended') THEN
        RAISE EXCEPTION 'Invalid status';
    END IF;

    IF p_role = 'member' AND p_is_owner = TRUE THEN
        RAISE EXCEPTION 'Members cannot be owners';
    END IF;

    RETURN QUERY
    INSERT INTO users_v2 (
        first_name, last_name, email, password_hash, cpf, phone, birth_date,
        company_id, status, role, is_owner,
        terms_accepted_at, privacy_policy_accepted_at, created_at, updated_at
    ) VALUES (
        p_first_name, p_last_name, p_email, p_password_hash, p_cpf, p_phone, p_birth_date,
        p_company_id, p_status, p_role, p_is_owner,
        NOW(), NOW(), NOW(), NOW()
    )
    RETURNING 
        users_v2.id, users_v2.email, users_v2.first_name, users_v2.last_name,
        users_v2.company_id, users_v2.role, users_v2.status, users_v2.is_owner,
        users_v2.created_at;
END;
$$;


--
-- Name: create_user_account(character varying, character varying, character varying, character varying, character varying, character varying, date, uuid, character varying, character varying, boolean, uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.create_user_account(p_first_name character varying, p_last_name character varying, p_email character varying, p_password_hash character varying, p_cpf character varying, p_phone character varying, p_birth_date date, p_company_id uuid DEFAULT NULL::uuid, p_status character varying DEFAULT 'pending'::character varying, p_role character varying DEFAULT 'member'::character varying, p_is_owner boolean DEFAULT false, p_accepted_terms_version uuid DEFAULT NULL::uuid) RETURNS TABLE(id uuid, email character varying, first_name character varying, last_name character varying, company_id uuid, role character varying, status character varying, is_owner boolean, created_at timestamp without time zone)
    LANGUAGE plpgsql SECURITY DEFINER
    AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM users_v2 WHERE users_v2.email = p_email) THEN
        RAISE EXCEPTION 'Email already exists';
    END IF;

    IF p_role NOT IN ('admin_company', 'member') THEN
        RAISE EXCEPTION 'Invalid role';
    END IF;

    IF p_status NOT IN ('active', 'pending', 'suspended') THEN
        RAISE EXCEPTION 'Invalid status';
    END IF;

    IF p_role = 'member' AND p_is_owner = TRUE THEN
        RAISE EXCEPTION 'Members cannot be owners';
    END IF;

    RETURN QUERY
    INSERT INTO users_v2 (
        first_name, last_name, email, password_hash, cpf, phone, birth_date,
        company_id, status, role, is_owner,
        terms_accepted_at, privacy_policy_accepted_at,
        accepted_terms_version,
        created_at, updated_at
    ) VALUES (
        p_first_name, p_last_name, p_email, p_password_hash, p_cpf, p_phone, p_birth_date,
        p_company_id, p_status, p_role, p_is_owner,
        NOW(), NOW(),
        p_accepted_terms_version,
        NOW(), NOW()
    )
    RETURNING 
        users_v2.id, users_v2.email, users_v2.first_name, users_v2.last_name,
        users_v2.company_id, users_v2.role, users_v2.status, users_v2.is_owner,
        users_v2.created_at;
END;
$$;


--
-- Name: credit_company_balance(uuid, numeric, text, text, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.credit_company_balance(p_company_id uuid, p_amount numeric, p_stripe_payment_id text DEFAULT NULL::text, p_type text DEFAULT 'topup'::text, p_description text DEFAULT NULL::text) RETURNS numeric
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
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


--
-- Name: FUNCTION credit_company_balance(p_company_id uuid, p_amount numeric, p_stripe_payment_id text, p_type text, p_description text); Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON FUNCTION public.credit_company_balance(p_company_id uuid, p_amount numeric, p_stripe_payment_id text, p_type text, p_description text) IS 'CRITICO-001: crédito atômico (balance += amount) com gate de idempotência Stripe (INSERT-first em credit_transactions) na MESMA transação. Substitui o read-modify-write de billing_core.add_credits.';


--
-- Name: debit_company_balance(uuid, numeric); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.debit_company_balance(p_company_id uuid, p_amount numeric) RETURNS numeric
    LANGUAGE plpgsql SECURITY DEFINER
    AS $$
DECLARE
    v_new_balance NUMERIC;
BEGIN
    UPDATE company_credits
    SET 
        balance_brl = balance_brl - p_amount,
        updated_at = NOW()
    WHERE company_id = p_company_id
    RETURNING balance_brl INTO v_new_balance;
    
    IF NOT FOUND THEN
        INSERT INTO company_credits (company_id, balance_brl, updated_at)
        VALUES (p_company_id, -p_amount, NOW())
        RETURNING balance_brl INTO v_new_balance;
    END IF;
    
    RETURN v_new_balance;
END;
$$;


--
-- Name: get_agent_ucp_capabilities(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.get_agent_ucp_capabilities(p_agent_id uuid) RETURNS TABLE(store_url text, capability text)
    LANGUAGE plpgsql
    AS $$
BEGIN
    RETURN QUERY
    SELECT 
        uc.store_url,
        unnest(uc.capabilities_enabled) as capability
    FROM public.ucp_connections uc
    WHERE uc.agent_id = p_agent_id
    AND uc.is_active = true;
END;
$$;


--
-- Name: get_token_usage_by_company(timestamp with time zone, timestamp with time zone); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.get_token_usage_by_company(start_date timestamp with time zone, end_date timestamp with time zone) RETURNS TABLE(company_id uuid, company_name text, total_calls bigint, total_input bigint, total_output bigint, total_cost numeric)
    LANGUAGE plpgsql SECURITY DEFINER
    AS $$
BEGIN
  RETURN QUERY
  SELECT
    t.company_id,
    COALESCE(c.company_name::TEXT, 'Sistema Interno') as company_name,
    COUNT(*) as total_calls,
    COALESCE(SUM(t.input_tokens), 0)::BIGINT as total_input,
    COALESCE(SUM(t.output_tokens), 0)::BIGINT as total_output,
    COALESCE(SUM(t.total_cost_usd), 0) as total_cost
  FROM token_usage_logs t
  LEFT JOIN companies c ON t.company_id = c.id
  WHERE t.created_at >= start_date AND t.created_at <= end_date
  GROUP BY t.company_id, c.company_name
  ORDER BY total_cost DESC;
END;
$$;


--
-- Name: get_token_usage_report(timestamp with time zone, timestamp with time zone); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.get_token_usage_report(start_date timestamp with time zone, end_date timestamp with time zone) RETURNS TABLE(company_name text, service_type text, model_name text, total_calls bigint, total_input bigint, total_output bigint, total_cost numeric)
    LANGUAGE plpgsql SECURITY DEFINER
    AS $$
BEGIN
  RETURN QUERY
  SELECT
    COALESCE(c.company_name::TEXT, 'Sistema Interno') as company_name,
    t.service_type::TEXT,
    t.model_name::TEXT,
    COUNT(*) as total_calls,
    COALESCE(SUM(t.input_tokens), 0)::BIGINT as total_input,
    COALESCE(SUM(t.output_tokens), 0)::BIGINT as total_output,
    COALESCE(SUM(t.total_cost_usd), 0) as total_cost
  FROM token_usage_logs t
  LEFT JOIN companies c ON t.company_id = c.id
  WHERE t.created_at >= start_date AND t.created_at <= end_date
  GROUP BY c.company_name, t.service_type, t.model_name
  ORDER BY total_cost DESC;
END;
$$;


--
-- Name: get_user_for_login(character varying); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.get_user_for_login(p_email character varying) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    AS $$
DECLARE
  v_result jsonb;
BEGIN
  SELECT jsonb_build_object(
    'id', u.id,
    'email', u.email,
    'password_hash', u.password_hash,
    'first_name', u.first_name,
    'last_name', u.last_name,
    'status', u.status,
    'plan_id', u.plan_id,
    'company_id', u.company_id,
    'failed_login_attempts', u.failed_login_attempts,
    'account_locked_until', u.account_locked_until,
    'company_status', c.status,
    'webhook_url', c.webhook_url
  )
  INTO v_result
  FROM users_v2 u
  LEFT JOIN companies c ON u.company_id = c.id
  WHERE u.email = p_email AND u.deleted_at IS NULL;

  RETURN v_result;
END;
$$;


--
-- Name: get_widget_agent_public(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.get_widget_agent_public(p_agent_id uuid) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
DECLARE
  v_agent record;
BEGIN
  SELECT
    a.id,
    a.company_id,
    a.widget_config
  INTO v_agent
  FROM public.agents a
  WHERE a.id = p_agent_id
    AND a.is_active = true
  LIMIT 1;

  IF NOT FOUND THEN
    RETURN NULL;
  END IF;

  RETURN jsonb_build_object(
    'id', v_agent.id,
    'company_id', v_agent.company_id,
    'widget_config', COALESCE(v_agent.widget_config, '{}'::jsonb)
  );
END;
$$;


--
-- Name: get_widget_messages_scoped(text, uuid, uuid, text, bigint, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.get_widget_messages_scoped(p_session_id text, p_company_id uuid, p_agent_id uuid, p_origin text, p_exp bigint, p_proof text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'extensions', 'private'
    AS $_$
DECLARE
  v_conversation record;
  v_agent record;
  v_messages jsonb;
  v_now_epoch bigint;
  v_secret text;
  v_expected_proof text;
  v_canonical_payload text;
BEGIN
  IF p_session_id IS NULL OR length(p_session_id) > 160 OR p_company_id IS NULL OR p_agent_id IS NULL THEN
    RETURN jsonb_build_object(
      'agent', NULL,
      'conversation', NULL,
      'messages', '[]'::jsonb
    );
  END IF;

  IF p_origin IS NULL
     OR length(p_origin) > 300
     OR p_exp IS NULL
     OR p_proof IS NULL
     OR length(p_proof) <> 64
     OR p_proof !~ '^[0-9a-fA-F]{64}$' THEN
    RETURN jsonb_build_object(
      'agent', NULL,
      'conversation', NULL,
      'messages', '[]'::jsonb
    );
  END IF;

  v_now_epoch := floor(extract(epoch from now()))::bigint;
  IF p_exp <= v_now_epoch OR p_exp > v_now_epoch + 300 THEN
    RETURN jsonb_build_object(
      'agent', NULL,
      'conversation', NULL,
      'messages', '[]'::jsonb
    );
  END IF;

  SELECT nullif(secret, '')
  INTO v_secret
  FROM private.app_runtime_secrets
  WHERE name = 'widget_hmac_secret';

  IF v_secret IS NULL THEN
    RAISE EXCEPTION 'widget rpc secret is not configured' USING ERRCODE = '28000';
  END IF;

  v_canonical_payload := concat_ws(
    E'\n',
    'widget-messages:v1',
    p_session_id,
    p_company_id::text,
    p_agent_id::text,
    p_origin,
    p_exp::text
  );
  v_expected_proof := encode(hmac(v_canonical_payload, v_secret, 'sha256'), 'hex');

  IF lower(p_proof) <> v_expected_proof THEN
    RETURN jsonb_build_object(
      'agent', NULL,
      'conversation', NULL,
      'messages', '[]'::jsonb
    );
  END IF;

  SELECT
    a.id,
    a.company_id,
    a.widget_config
  INTO v_agent
  FROM public.agents a
  WHERE a.id = p_agent_id
    AND a.company_id = p_company_id
    AND a.is_active = true
  LIMIT 1;

  IF NOT FOUND THEN
    RETURN jsonb_build_object(
      'agent', NULL,
      'conversation', NULL,
      'messages', '[]'::jsonb
    );
  END IF;

  SELECT
    c.id,
    c.status,
    c.company_id,
    c.agent_id
  INTO v_conversation
  FROM public.conversations c
  WHERE c.session_id = p_session_id
    AND c.company_id = p_company_id
    AND c.agent_id = p_agent_id
  LIMIT 1;

  IF NOT FOUND THEN
    RETURN jsonb_build_object(
      'agent', jsonb_build_object(
        'id', v_agent.id,
        'company_id', v_agent.company_id,
        'widget_config', COALESCE(v_agent.widget_config, '{}'::jsonb)
      ),
      'conversation', NULL,
      'messages', '[]'::jsonb
    );
  END IF;

  SELECT COALESCE(
    jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'role', m.role,
        'content', m.content,
        'image_url', m.image_url,
        'audio_url', m.audio_url,
        'created_at', m.created_at,
        'sender_user_id', m.sender_user_id,
        'sender',
          CASE
            WHEN u.id IS NULL THEN NULL
            ELSE jsonb_build_object(
              'first_name', u.first_name,
              'last_name', u.last_name
            )
          END
      )
      ORDER BY m.created_at ASC
    ),
    '[]'::jsonb
  )
  INTO v_messages
  FROM public.messages m
  LEFT JOIN public.users_v2 u
    ON u.id = m.sender_user_id
   AND u.company_id = p_company_id
  WHERE m.conversation_id = v_conversation.id
    AND m.created_at >= now() - interval '60 minutes';

  RETURN jsonb_build_object(
    'agent', jsonb_build_object(
      'id', v_agent.id,
      'company_id', v_agent.company_id,
      'widget_config', COALESCE(v_agent.widget_config, '{}'::jsonb)
    ),
    'conversation', jsonb_build_object(
      'id', v_conversation.id,
      'status', v_conversation.status,
      'company_id', v_conversation.company_id,
      'agent_id', v_conversation.agent_id
    ),
    'messages', v_messages
  );
END;
$_$;


--
-- Name: increment_conversation_unread(uuid, uuid, text, timestamp with time zone); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.increment_conversation_unread(p_conversation_id uuid, p_company_id uuid, p_preview text, p_last_message_at timestamp with time zone) RETURNS void
    LANGUAGE sql
    AS $$
    update public.conversations
       set unread_count        = coalesce(unread_count, 0) + 1,
           last_message_preview = p_preview,
           last_message_at      = p_last_message_at
     where id = p_conversation_id
       and company_id = p_company_id;
$$;


--
-- Name: process_token_usage_outbox(integer, integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.process_token_usage_outbox(p_limit integer DEFAULT 100, p_stale_minutes integer DEFAULT 5) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
DECLARE
    r RECORD;
    v_done int := 0;
BEGIN
    FOR r IN
        SELECT * FROM public.token_usage_outbox
         WHERE dead_at IS NULL
           AND (claimed_at IS NULL OR claimed_at < now() - make_interval(mins => p_stale_minutes))
         ORDER BY created_at
         LIMIT p_limit
         FOR UPDATE SKIP LOCKED
    LOOP
        BEGIN
            INSERT INTO public.token_usage_logs (
                company_id, agent_id, service_type, model_name, input_tokens, output_tokens,
                total_cost_usd, details, created_at, billed,
                cache_creation_tokens, cache_read_tokens, cached_tokens, idempotency_key
            )
            SELECT
                (r.payload->>'company_id')::uuid,
                NULLIF(r.payload->>'agent_id', '')::uuid,
                r.payload->>'service_type',
                r.payload->>'model_name',
                COALESCE((r.payload->>'input_tokens')::int, 0),
                COALESCE((r.payload->>'output_tokens')::int, 0),
                COALESCE((r.payload->>'total_cost_usd')::numeric, 0),
                COALESCE(r.payload->'details', '{}'::jsonb),
                COALESCE((r.payload->>'created_at')::timestamptz, now()),
                false,
                COALESCE((r.payload->>'cache_creation_tokens')::int, 0),
                COALESCE((r.payload->>'cache_read_tokens')::int, 0),
                COALESCE((r.payload->>'cached_tokens')::int, 0),
                r.idempotency_key
            ON CONFLICT (idempotency_key) DO NOTHING;

            DELETE FROM public.token_usage_outbox WHERE id = r.id;
            v_done := v_done + 1;
        EXCEPTION WHEN OTHERS THEN
            IF SQLSTATE LIKE '40%'
               OR SQLSTATE LIKE '08%'
               OR SQLSTATE IN ('55P03', '57014', '53300') THEN
                UPDATE public.token_usage_outbox
                   SET last_error = '[transient ' || SQLSTATE || '] ' || SQLERRM
                 WHERE id = r.id;
            ELSE
                UPDATE public.token_usage_outbox
                   SET attempts = r.attempts + 1,
                       last_error = '[' || SQLSTATE || '] ' || SQLERRM,
                       claimed_at = now(),
                       dead_at = CASE WHEN r.attempts + 1 >= r.max_attempts THEN now() ELSE NULL END
                 WHERE id = r.id;
                IF r.attempts + 1 >= r.max_attempts THEN
                    RAISE WARNING '[token_usage_outbox] DEAD-LETTER id=% idem=% após % tentativas (SQLSTATE %): %',
                        r.id, r.idempotency_key, r.attempts + 1, SQLSTATE, SQLERRM;
                END IF;
            END IF;
        END;
    END LOOP;

    RETURN v_done;
END;
$$;


--
-- Name: reset_company_balance(uuid, numeric, text, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.reset_company_balance(p_company_id uuid, p_amount numeric, p_stripe_payment_id text DEFAULT NULL::text, p_description text DEFAULT NULL::text) RETURNS numeric
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
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


--
-- Name: FUNCTION reset_company_balance(p_company_id uuid, p_amount numeric, p_stripe_payment_id text, p_description text); Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON FUNCTION public.reset_company_balance(p_company_id uuid, p_amount numeric, p_stripe_payment_id text, p_description text) IS 'CRITICO-001: reset atômico de saldo (balance = amount) com gate de idempotência na MESMA transação. Substitui o read-modify-write de billing_core.reset_credits.';


--
-- Name: rpc_attendance_transition(text, uuid, uuid, text, uuid, text, uuid, uuid, jsonb, timestamp with time zone, timestamp with time zone, text, jsonb, timestamp with time zone); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.rpc_attendance_transition(p_action text, p_company_id uuid, p_conversation_id uuid DEFAULT NULL::uuid, p_session_id text DEFAULT NULL::text, p_agent_id uuid DEFAULT NULL::uuid, p_actor_type text DEFAULT NULL::text, p_actor_user_id uuid DEFAULT NULL::uuid, p_actor_agent_id uuid DEFAULT NULL::uuid, p_payload jsonb DEFAULT '{}'::jsonb, p_first_response_deadline timestamp with time zone DEFAULT NULL::timestamp with time zone, p_resolution_deadline timestamp with time zone DEFAULT NULL::timestamp with time zone, p_sla_level text DEFAULT NULL::text, p_policy_snapshot jsonb DEFAULT NULL::jsonb, p_started_at timestamp with time zone DEFAULT now()) RETURNS jsonb
    LANGUAGE plpgsql
    AS $$
DECLARE
    v_conv          public.conversations%ROWTYPE;
    v_prev_status   text;
    v_new_status    text;
    v_session_id    uuid;
    v_session_status text;
    v_sla_id        uuid;
    v_event_id      uuid;
    v_now           timestamptz := now();
    v_idem          text;
    v_close_kind    text;        -- 'CLOSED' | 'RESOLVED'
    v_event_type    text;
    v_payload       jsonb := coalesce(p_payload, '{}'::jsonb);
BEGIN
    -- ---- Resolução + lock da conversa (tenancy: falha fechada) ----
    IF p_conversation_id IS NOT NULL THEN
        SELECT * INTO v_conv
        FROM public.conversations
        WHERE id = p_conversation_id
        FOR UPDATE;
    ELSIF p_session_id IS NOT NULL THEN
        -- §10.1: escopo session_id + company_id + agent_id.
        SELECT * INTO v_conv
        FROM public.conversations
        WHERE session_id = p_session_id
          AND company_id = p_company_id
          AND (p_agent_id IS NULL OR agent_id = p_agent_id)
        ORDER BY created_at DESC
        LIMIT 1
        FOR UPDATE;
    ELSE
        RAISE EXCEPTION 'attendance_transition: missing conversation identifier'
            USING ERRCODE = '22023';
    END IF;

    IF v_conv.id IS NULL THEN
        RAISE EXCEPTION 'attendance_transition: conversation not found'
            USING ERRCODE = 'P0002';
    END IF;

    -- Tenancy: a conversa deve pertencer ao company_id informado (§7.10 regra 5).
    IF v_conv.company_id IS DISTINCT FROM p_company_id THEN
        RAISE EXCEPTION 'attendance_transition: tenancy violation (company mismatch)'
            USING ERRCODE = '42501';
    END IF;

    v_prev_status := v_conv.status;
    v_new_status  := v_prev_status;
    v_session_id  := v_conv.current_attendance_session_id;

    -- ===================================================================== --
    -- Ações
    -- ===================================================================== --
    IF p_action = 'request_handoff' THEN
        -- §6.3: open | RETURNED_TO_AI -> HUMAN_REQUESTED (e idempotente se já requested).
        IF v_prev_status NOT IN ('open', 'RETURNED_TO_AI', 'HUMAN_REQUESTED') THEN
            RAISE EXCEPTION 'attendance_transition: invalid transition % -> HUMAN_REQUESTED', v_prev_status
                USING ERRCODE = 'P0001';
        END IF;

        -- §7.2: human_requested_by_type só aceita ('agent','human','system'). 'customer'
        -- é actor válido de eventos mas NÃO de quem solicita handoff — erro estruturado
        -- em vez de deixar o CHECK do banco abortar com erro genérico.
        IF p_actor_type IS NOT NULL AND p_actor_type NOT IN ('agent', 'human', 'system') THEN
            RAISE EXCEPTION 'attendance_transition: invalid handoff actor_type %', p_actor_type
                USING ERRCODE = 'P0001';
        END IF;

        -- Sessão idempotente / concorrência-safe (§7.2).
        v_session_id := public._attendance_ensure_open_session(
            v_conv.id, p_company_id, coalesce(p_agent_id, v_conv.agent_id),
            'human_requested', v_payload, p_actor_type, p_actor_agent_id, p_actor_user_id
        );

        UPDATE public.attendance_sessions
           SET status = 'human_requested',
               human_requested_at = coalesce(human_requested_at, v_now),
               human_requested_by_type = coalesce(p_actor_type, 'agent'),
               human_requested_by_agent_id = p_actor_agent_id,
               human_requested_by_user_id = p_actor_user_id,
               human_request_reason = coalesce(v_payload->>'reason', human_request_reason),
               updated_at = v_now
         WHERE id = v_session_id;

        v_new_status := 'HUMAN_REQUESTED';
        UPDATE public.conversations
           SET status = v_new_status,
               current_attendance_session_id = v_session_id,
               human_requested_at = coalesce(human_requested_at, v_now)
         WHERE id = v_conv.id;

        v_idem := v_session_id::text || ':handoff_requested';
        v_event_id := public._attendance_record_event(
            v_conv.id, v_session_id, p_company_id, coalesce(p_agent_id, v_conv.agent_id),
            'handoff_requested', p_actor_type, p_actor_user_id, p_actor_agent_id, v_payload, v_idem
        );

        -- Contrato de SLA: cria attendance_sla SÓ quando os 4 params estão preenchidos.
        IF p_first_response_deadline IS NOT NULL AND p_resolution_deadline IS NOT NULL
           AND p_sla_level IS NOT NULL AND p_policy_snapshot IS NOT NULL THEN
            v_sla_id := public._attendance_create_sla(
                v_session_id, v_conv.id, p_company_id, p_sla_level,
                coalesce(p_started_at, v_now),
                p_first_response_deadline, p_resolution_deadline, p_policy_snapshot
            );
        END IF;

        -- OUTBOX MESMO-COMMIT (S4/§8.3/§11.1): o handoff via tool/regra (request_handoff)
        -- NÃO tem dono humano (claim é a tomada manual e tem ação própria, que NÃO
        -- notifica). Por isso só request_handoff enfileira notification_deliveries
        -- pendentes — na MESMA transação do handoff. Falha de ENVIO (depois, pelo
        -- worker) nunca desfaz o handoff; a linha pending fica visível no card.
        PERFORM public._attendance_enqueue_handoff_notifications(
            v_conv.id, v_session_id, p_company_id, coalesce(p_agent_id, v_conv.agent_id)
        );

    ELSIF p_action = 'claim' THEN
        -- §6.3: open | HUMAN_REQUESTED -> HUMAN_ACTIVE (atômico, sem notificar).
        IF v_prev_status NOT IN ('open', 'HUMAN_REQUESTED') THEN
            RAISE EXCEPTION 'attendance_transition: invalid transition % -> HUMAN_ACTIVE (claim)', v_prev_status
                USING ERRCODE = 'P0001';
        END IF;

        v_session_id := public._attendance_ensure_open_session(
            v_conv.id, p_company_id, coalesce(p_agent_id, v_conv.agent_id),
            'human_active', v_payload, p_actor_type, p_actor_agent_id, p_actor_user_id
        );

        UPDATE public.attendance_sessions
           SET status = 'human_active',
               human_taken_at = coalesce(human_taken_at, v_now),
               human_taken_by_user_id = coalesce(p_actor_user_id, human_taken_by_user_id),
               first_human_response_at = coalesce(first_human_response_at, v_now),
               updated_at = v_now
         WHERE id = v_session_id;

        v_new_status := 'HUMAN_ACTIVE';
        UPDATE public.conversations
           SET status = v_new_status,
               current_attendance_session_id = v_session_id,
               assigned_user_id = coalesce(p_actor_user_id, assigned_user_id),
               human_taken_at = coalesce(human_taken_at, v_now),
               first_human_response_at = coalesce(first_human_response_at, v_now)
         WHERE id = v_conv.id;

        v_idem := v_session_id::text || ':human_claimed';
        v_event_id := public._attendance_record_event(
            v_conv.id, v_session_id, p_company_id, coalesce(p_agent_id, v_conv.agent_id),
            'human_claimed', p_actor_type, p_actor_user_id, p_actor_agent_id, v_payload, v_idem
        );

        IF p_first_response_deadline IS NOT NULL AND p_resolution_deadline IS NOT NULL
           AND p_sla_level IS NOT NULL AND p_policy_snapshot IS NOT NULL THEN
            v_sla_id := public._attendance_create_sla(
                v_session_id, v_conv.id, p_company_id, p_sla_level,
                coalesce(p_started_at, v_now),
                p_first_response_deadline, p_resolution_deadline, p_policy_snapshot
            );
        END IF;

        -- §6.3 linha 262: claim 'marca a primeira resposta como cumprida pelo próprio
        -- ato de assumir'. Marca o marco de SLA de 1ª resposta no MESMO commit do claim
        -- (cobre tanto o SLA recém-criado acima quanto um já existente), se houver SLA.
        UPDATE public.attendance_sla
           SET first_response_status = 'met',
               first_response_at = coalesce(first_response_at, v_now),
               updated_at = v_now
         WHERE attendance_session_id = v_session_id
           AND first_response_status = 'pending';
        IF FOUND THEN
            INSERT INTO public.sla_events (
                attendance_sla_id, attendance_session_id, conversation_id, company_id,
                event_type, actor_type
            )
            SELECT s.id, v_session_id, v_conv.id, p_company_id,
                   'first_response_met', coalesce(p_actor_type, 'human')
            FROM public.attendance_sla s
            WHERE s.attendance_session_id = v_session_id
            ON CONFLICT (attendance_session_id, event_type)
                WHERE event_type IN (
                    'first_response_met', 'first_response_missed', 'at_risk_50pct',
                    'critical_75pct', 'resolution_breached', 'resolution_met',
                    'resolution_missed')
            DO NOTHING;
        END IF;

    ELSIF p_action = 'return_to_ai' THEN
        -- §6.3: HUMAN_REQUESTED | HUMAN_ACTIVE -> RETURNED_TO_AI (sessão encerra),
        -- mas o estado operacional final da conversa é 'open' (§6.3 regra).
        IF v_prev_status NOT IN ('HUMAN_REQUESTED', 'HUMAN_ACTIVE') THEN
            RAISE EXCEPTION 'attendance_transition: invalid transition % -> RETURNED_TO_AI', v_prev_status
                USING ERRCODE = 'P0001';
        END IF;

        IF v_session_id IS NOT NULL THEN
            UPDATE public.attendance_sessions
               SET status = 'returned_to_ai',
                   returned_to_ai_at = v_now,
                   returned_to_ai_by_user_id = p_actor_user_id,
                   updated_at = v_now
             WHERE id = v_session_id;
        END IF;

        v_new_status := 'open';
        UPDATE public.conversations
           SET status = v_new_status,
               current_attendance_session_id = NULL,
               assigned_user_id = NULL,
               returned_to_ai_at = v_now
         WHERE id = v_conv.id;

        v_idem := coalesce(v_session_id::text, v_conv.id::text) || ':returned_to_ai';
        v_event_id := public._attendance_record_event(
            v_conv.id, v_session_id, p_company_id, coalesce(p_agent_id, v_conv.agent_id),
            'returned_to_ai', p_actor_type, p_actor_user_id, p_actor_agent_id, v_payload, v_idem
        );

    ELSIF p_action IN ('resolve', 'close') THEN
        -- §6.3: a maioria dos estados pode resolver/fechar.
        IF v_prev_status NOT IN ('open', 'HUMAN_REQUESTED', 'HUMAN_ACTIVE',
                                 'PENDING_CUSTOMER', 'RETURNED_TO_AI') THEN
            RAISE EXCEPTION 'attendance_transition: invalid transition % -> %', v_prev_status, p_action
                USING ERRCODE = 'P0001';
        END IF;

        -- S6 (D)/§7.1: closed_by_type só aceita ('human','agent','system'). 'customer'
        -- é actor válido de eventos mas NÃO de quem resolve/fecha. Guard explícito
        -- (espelha o de request_handoff) para que QUALQUER caller direto — incl. o
        -- Next via supabaseAdmin.rpc — receba erro estruturado (ERRCODE P0001) em vez
        -- da violação CRUA do CHECK de closed_by_type (erro genérico 23514).
        IF p_actor_type IS NOT NULL AND p_actor_type NOT IN ('human', 'agent', 'system') THEN
            RAISE EXCEPTION 'attendance_transition: invalid close/resolve actor_type %', p_actor_type
                USING ERRCODE = 'P0001';
        END IF;

        v_close_kind := CASE WHEN p_action = 'resolve' THEN 'RESOLVED' ELSE 'CLOSED' END;
        v_new_status := v_close_kind;

        IF v_session_id IS NOT NULL THEN
            UPDATE public.attendance_sessions
               SET status = CASE WHEN p_action = 'resolve' THEN 'resolved' ELSE 'closed' END,
                   resolved_at = CASE WHEN p_action = 'resolve' THEN v_now ELSE resolved_at END,
                   closed_at = v_now,
                   closed_by_type = p_actor_type,
                   closed_by_user_id = p_actor_user_id,
                   closed_by_agent_id = p_actor_agent_id,
                   close_reason = v_payload->>'reason',
                   close_summary = v_payload->>'summary',
                   updated_at = v_now
             WHERE id = v_session_id;
        END IF;

        UPDATE public.conversations
           SET status = v_new_status,
               current_attendance_session_id = NULL,
               resolved_at = CASE WHEN p_action = 'resolve' THEN v_now ELSE resolved_at END,
               closed_at = v_now,
               closed_by_type = p_actor_type,
               closed_by_user_id = p_actor_user_id,
               closed_by_agent_id = p_actor_agent_id,
               close_reason = v_payload->>'reason',
               close_summary = v_payload->>'summary'
         WHERE id = v_conv.id;

        -- SETTLE SLA NO MESMO COMMIT (§7.5/§15): ao resolver/fechar, marcos de SLA
        -- ainda 'pending' precisam de estado TERMINAL. Sem isso, o attendance_sla
        -- fica 'pending' enquanto a sessão/conversa já está RESOLVED/CLOSED, e o
        -- worker de SLA (run_check_sla) — que seleciona candidatos só por
        -- first_response_status/resolution_status='pending' — re-processa essa linha
        -- a cada tick, marcando first_response_missed/resolution_breached DEPOIS do
        -- fechamento (evento espúrio pós-encerramento). Espelha o padrão do claim:
        -- update guardado por 'pending' + sla_events one-shot com ON CONFLICT.
        --   first_response: pending -> 'met' se já houve 1ª resposta humana
        --     (first_response_at preenchido pelo claim), senão 'missed'.
        --   resolution: pending -> 'met' no resolve, 'missed' no close.
        IF v_session_id IS NOT NULL THEN
            UPDATE public.attendance_sla
               SET first_response_status = CASE
                       WHEN first_response_status = 'pending'
                            AND first_response_at IS NOT NULL THEN 'met'
                       WHEN first_response_status = 'pending' THEN 'missed'
                       ELSE first_response_status END,
                   resolution_status = CASE
                       WHEN resolution_status = 'pending' AND p_action = 'resolve' THEN 'met'
                       WHEN resolution_status = 'pending' THEN 'missed'
                       ELSE resolution_status END,
                   resolved_at = CASE
                       WHEN resolution_status = 'pending' AND p_action = 'resolve'
                            AND resolved_at IS NULL THEN v_now
                       ELSE resolved_at END,
                   updated_at = v_now
             WHERE attendance_session_id = v_session_id
               AND (first_response_status = 'pending'
                    OR resolution_status = 'pending');

            -- sla_events one-shot dos marcos recém-settled (idempotentes §7.6).
            INSERT INTO public.sla_events (
                attendance_sla_id, attendance_session_id, conversation_id, company_id,
                event_type, actor_type
            )
            SELECT s.id, v_session_id, v_conv.id, p_company_id, e.event_type, 'system'
            FROM public.attendance_sla s
            CROSS JOIN LATERAL (
                VALUES
                    (CASE WHEN s.first_response_status = 'met'
                          THEN 'first_response_met'
                          WHEN s.first_response_status = 'missed'
                          THEN 'first_response_missed' END),
                    (CASE WHEN s.resolution_status = 'met' THEN 'resolution_met'
                          WHEN s.resolution_status = 'missed' THEN 'resolution_missed'
                          WHEN s.resolution_status = 'breached'
                          THEN 'resolution_breached' END)
            ) AS e(event_type)
            WHERE s.attendance_session_id = v_session_id
              AND e.event_type IS NOT NULL
            ON CONFLICT (attendance_session_id, event_type)
                WHERE event_type IN (
                    'first_response_met', 'first_response_missed', 'at_risk_50pct',
                    'critical_75pct', 'resolution_breached', 'resolution_met',
                    'resolution_missed')
            DO NOTHING;
        END IF;

        -- Evento: resolved_by_* / closed_by_* conforme actor.
        IF p_action = 'resolve' THEN
            v_event_type := CASE WHEN p_actor_type = 'agent' THEN 'resolved_by_agent'
                                 ELSE 'resolved_by_human' END;
        ELSE
            -- Auto-close por inatividade (§16/§8.5): o timer marca close_kind=timeout
            -- no payload para distinguir o auto-close de um fechamento manual do
            -- sistema. Sem o marcador, system permanece closed_by_system.
            v_event_type := CASE
                WHEN p_actor_type = 'agent' THEN 'closed_by_agent'
                WHEN p_actor_type = 'system'
                     AND v_payload->>'close_kind' = 'timeout' THEN 'timeout_closed'
                WHEN p_actor_type = 'system' THEN 'closed_by_system'
                ELSE 'closed_by_human' END;
        END IF;

        v_idem := coalesce(v_session_id::text, v_conv.id::text) || ':' || v_event_type;
        v_event_id := public._attendance_record_event(
            v_conv.id, v_session_id, p_company_id, coalesce(p_agent_id, v_conv.agent_id),
            v_event_type, p_actor_type, p_actor_user_id, p_actor_agent_id, v_payload, v_idem
        );

    ELSIF p_action = 'reopen' THEN
        -- §6.2/§6.3: RESOLVED | CLOSED -> open. Cria NOVA sessão.
        IF v_prev_status NOT IN ('RESOLVED', 'CLOSED') THEN
            RAISE EXCEPTION 'attendance_transition: invalid transition % -> open (reopen)', v_prev_status
                USING ERRCODE = 'P0001';
        END IF;

        -- reopened_by_admin (actor humano explícito) vs reopened_by_customer.
        v_event_type := CASE WHEN p_actor_type = 'customer'
                             THEN 'reopened_by_customer'
                             ELSE 'reopened_by_admin' END;

        v_session_id := public._attendance_ensure_open_session(
            v_conv.id, p_company_id, coalesce(p_agent_id, v_conv.agent_id),
            'open', v_payload, p_actor_type, p_actor_agent_id, p_actor_user_id
        );

        v_new_status := 'open';
        UPDATE public.conversations
           SET status = v_new_status,
               current_attendance_session_id = v_session_id,
               assigned_user_id = NULL,
               resolved_at = NULL,
               closed_at = NULL,
               closed_by_type = NULL,
               closed_by_user_id = NULL,
               closed_by_agent_id = NULL,
               close_reason = NULL,
               close_summary = NULL
         WHERE id = v_conv.id;

        v_idem := v_session_id::text || ':' || v_event_type;
        v_event_id := public._attendance_record_event(
            v_conv.id, v_session_id, p_company_id, coalesce(p_agent_id, v_conv.agent_id),
            v_event_type, p_actor_type, p_actor_user_id, p_actor_agent_id, v_payload, v_idem
        );

    ELSIF p_action = 'record_human_message' THEN
        -- §9.1/§6.3: transição COMPOSTA atômica. Se HUMAN_REQUESTED, o primeiro envio
        -- humano ASSUME o atendimento (HUMAN_REQUESTED ->(assume)-> HUMAN_ACTIVE: seta
        -- assigned_user_id/human_taken_at/first_human_response_at e emite o evento
        -- one-shot 'human_claimed', espelhando a semântica de 'claim', §9.1 linha 1057)
        -- e SÓ ENTÃO aplica o gatilho de aguardar cliente (humano enviou -> PENDING_CUSTOMER).
        -- O estado HUMAN_ACTIVE intermediário não persiste como linha duradoura, mas a
        -- assunção fica auditável na timeline via 'human_claimed'. A partir de HUMAN_ACTIVE
        -- ou PENDING_CUSTOMER, apenas reafirma PENDING_CUSTOMER. Mensagem repetível (idem NULL).
        --
        -- NÃO-QUEBRA (§8.1 / S6 critério "caller atual continua funcional"): nos status em
        -- que a IA NÃO está bloqueada por atendimento humano (`open`/`RETURNED_TO_AI`), o
        -- caller legado (chat atual, que ainda não migrou para a UI de S9/S10) pode enviar
        -- uma mensagem humana sem ter passado por `request_handoff`/`claim` (ex.: admin digita
        -- no composer antes de "Assumir", ou logo após `return_to_ai`). Em vez de rejeitar com
        -- P0001 (o que PERDERIA a mensagem na rota legada, que chama a RPC ANTES do insert),
        -- tratamos esse caso como PERSISTÊNCIA SIMPLES: gravamos last_human_message_at + evento
        -- `human_message_sent`, SEM forçar transição de status (a IA continua no comando).
        -- Espelha o padrão tolerante de `record_customer_message` (ramo ELSE) e mantém a SPEC
        -- §6.3 intacta para os 3 status de atendimento humano. Status REALMENTE inválidos
        -- (RESOLVED/CLOSED) continuam rejeitados com P0001.
        IF v_prev_status IN ('open', 'RETURNED_TO_AI') THEN
            -- Persistência simples, sem transição (IA não bloqueada). NÃO seta
            -- first_human_response_at/human_taken_at (não há atendimento humano em curso).
            UPDATE public.conversations
               SET last_human_message_at = v_now
             WHERE id = v_conv.id;

            v_event_id := public._attendance_record_event(
                v_conv.id, v_session_id, p_company_id, coalesce(p_agent_id, v_conv.agent_id),
                'human_message_sent', coalesce(p_actor_type, 'human'),
                p_actor_user_id, p_actor_agent_id, v_payload, NULL
            );

            RETURN jsonb_build_object(
                'status', v_new_status,
                'previous_status', v_prev_status,
                'conversation_id', v_conv.id,
                'attendance_session_id', v_session_id,
                'attendance_sla_id', v_sla_id,
                'event_id', v_event_id
            );
        END IF;

        IF v_prev_status NOT IN ('HUMAN_REQUESTED', 'HUMAN_ACTIVE', 'PENDING_CUSTOMER') THEN
            RAISE EXCEPTION 'attendance_transition: record_human_message invalid in status %', v_prev_status
                USING ERRCODE = 'P0001';
        END IF;

        IF v_session_id IS NOT NULL THEN
            UPDATE public.attendance_sessions
               SET status = 'pending_customer',
                   human_taken_at = coalesce(human_taken_at, v_now),
                   human_taken_by_user_id = coalesce(human_taken_by_user_id, p_actor_user_id),
                   first_human_response_at = coalesce(first_human_response_at, v_now),
                   updated_at = v_now
             WHERE id = v_session_id;
        END IF;

        v_new_status := 'PENDING_CUSTOMER';
        UPDATE public.conversations
           SET status = v_new_status,
               human_taken_at = coalesce(human_taken_at, v_now),
               assigned_user_id = coalesce(assigned_user_id, p_actor_user_id),
               first_human_response_at = coalesce(first_human_response_at, v_now),
               last_human_message_at = v_now,
               customer_waiting_since = NULL
         WHERE id = v_conv.id;

        -- Marco de assunção: quando este é o primeiro envio humano sobre uma sessão
        -- que estava aguardando responsável (HUMAN_REQUESTED), grava o evento
        -- one-shot 'human_claimed' (idempotency_key '{session}:human_claimed', a MESMA
        -- chave usada por 'claim') ANTES do human_message_sent. Se o atendimento já
        -- foi assumido (claim prévio), o ON CONFLICT torna isto um no-op idempotente.
        IF v_prev_status = 'HUMAN_REQUESTED' AND v_session_id IS NOT NULL THEN
            v_idem := v_session_id::text || ':human_claimed';
            PERFORM public._attendance_record_event(
                v_conv.id, v_session_id, p_company_id, coalesce(p_agent_id, v_conv.agent_id),
                'human_claimed', p_actor_type, p_actor_user_id, p_actor_agent_id, v_payload, v_idem
            );

            -- Marca o marco de 1ª resposta de SLA cumprido no MESMO commit da assunção
            -- (§6.3 linha 262: o ato de assumir cumpre a primeira resposta), se houver SLA.
            UPDATE public.attendance_sla
               SET first_response_status = 'met',
                   first_response_at = coalesce(first_response_at, v_now),
                   updated_at = v_now
             WHERE attendance_session_id = v_session_id
               AND first_response_status = 'pending';
            IF FOUND THEN
                INSERT INTO public.sla_events (
                    attendance_sla_id, attendance_session_id, conversation_id, company_id,
                    event_type, actor_type
                )
                SELECT s.id, v_session_id, v_conv.id, p_company_id,
                       'first_response_met', coalesce(p_actor_type, 'human')
                FROM public.attendance_sla s
                WHERE s.attendance_session_id = v_session_id
                ON CONFLICT (attendance_session_id, event_type)
                    WHERE event_type IN (
                        'first_response_met', 'first_response_missed', 'at_risk_50pct',
                        'critical_75pct', 'resolution_breached', 'resolution_met',
                        'resolution_missed')
                DO NOTHING;
            END IF;
        END IF;

        v_event_id := public._attendance_record_event(
            v_conv.id, v_session_id, p_company_id, coalesce(p_agent_id, v_conv.agent_id),
            'human_message_sent', p_actor_type, p_actor_user_id, p_actor_agent_id, v_payload, NULL
        );

    ELSIF p_action = 'record_customer_message' THEN
        -- §6.3 boundary: se PENDING_CUSTOMER, promove internamente -> HUMAN_ACTIVE.
        -- Em qualquer outro status, só grava timestamps/evento sem mudar status.
        IF v_prev_status = 'PENDING_CUSTOMER' THEN
            v_new_status := 'HUMAN_ACTIVE';
            IF v_session_id IS NOT NULL THEN
                UPDATE public.attendance_sessions
                   SET status = 'human_active', updated_at = v_now
                 WHERE id = v_session_id;
            END IF;
            UPDATE public.conversations
               SET status = v_new_status,
                   last_customer_message_at = v_now,
                   customer_waiting_since = NULL
             WHERE id = v_conv.id;
        ELSE
            UPDATE public.conversations
               SET last_customer_message_at = v_now,
                   customer_waiting_since = coalesce(customer_waiting_since, v_now)
             WHERE id = v_conv.id;
        END IF;

        v_event_id := public._attendance_record_event(
            v_conv.id, v_session_id, p_company_id, coalesce(p_agent_id, v_conv.agent_id),
            'customer_message_received', coalesce(p_actor_type, 'customer'),
            p_actor_user_id, p_actor_agent_id, v_payload, NULL
        );

    ELSIF p_action = 'record_ai_message' THEN
        -- Repetível (idem NULL); não muda status.
        UPDATE public.conversations
           SET last_ai_message_at = v_now
         WHERE id = v_conv.id;
        v_event_id := public._attendance_record_event(
            v_conv.id, v_session_id, p_company_id, coalesce(p_agent_id, v_conv.agent_id),
            'ai_message_sent', coalesce(p_actor_type, 'agent'),
            p_actor_user_id, p_actor_agent_id, v_payload, NULL
        );

    ELSIF p_action IN ('add_note', 'create_event') THEN
        v_event_type := CASE WHEN p_action = 'add_note'
                             THEN 'note_added'
                             ELSE coalesce(v_payload->>'event_type', 'note_added') END;
        v_event_id := public._attendance_record_event(
            v_conv.id, v_session_id, p_company_id, coalesce(p_agent_id, v_conv.agent_id),
            v_event_type, p_actor_type, p_actor_user_id, p_actor_agent_id, v_payload, NULL
        );

    ELSE
        RAISE EXCEPTION 'attendance_transition: unknown action %', p_action
            USING ERRCODE = '22023';
    END IF;

    RETURN jsonb_build_object(
        'status', v_new_status,
        'previous_status', v_prev_status,
        'conversation_id', v_conv.id,
        'attendance_session_id', v_session_id,
        'attendance_sla_id', v_sla_id,
        'event_id', v_event_id
    );
END;
$$;


--
-- Name: rpc_list_contacts(uuid, text, text, timestamp with time zone, timestamp with time zone, integer, integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.rpc_list_contacts(p_company_id uuid, p_search text DEFAULT NULL::text, p_channel text DEFAULT NULL::text, p_created_from timestamp with time zone DEFAULT NULL::timestamp with time zone, p_created_to timestamp with time zone DEFAULT NULL::timestamp with time zone, p_limit integer DEFAULT NULL::integer, p_offset integer DEFAULT 0) RETURNS TABLE(contact_key text, user_id uuid, name text, phone text, email text, channel text, created_at timestamp with time zone, last_seen timestamp with time zone, conversation_count bigint, total_count bigint)
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
    WITH agg AS (
        SELECT
            COALESCE(c.user_id::text, NULLIF(c.user_phone, ''), c.session_id) AS contact_key,
            -- Postgres não tem MAX(uuid); como o grupo é chaveado por user_id::text
            -- (user_id é NOT NULL), o valor é constante no grupo: agrega como text e
            -- faz cast de volta p/ uuid.
            (MAX(c.user_id::text))::uuid            AS user_id,
            MAX(c.user_name)                        AS name,
            MAX(c.user_phone)                       AS phone,
            MAX(COALESCE(l.email, u.email))         AS email,
            MAX(c.channel)                          AS channel,
            MIN(c.created_at)                       AS created_at,
            MAX(c.last_message_at)                  AS last_seen,
            COUNT(*)                                AS conversation_count
        FROM public.conversations c
        LEFT JOIN public.leads    l ON l.id = c.user_id AND l.company_id = c.company_id
        LEFT JOIN public.users_v2 u ON u.id = c.user_id AND u.company_id = c.company_id
        WHERE c.company_id = p_company_id
          AND (p_channel      IS NULL OR c.channel = p_channel)
          AND (p_created_from IS NULL OR c.created_at >= p_created_from)
          AND (p_created_to   IS NULL OR c.created_at <  p_created_to)
        GROUP BY COALESCE(c.user_id::text, NULLIF(c.user_phone, ''), c.session_id)
    )
    SELECT
        agg.contact_key,
        agg.user_id,
        agg.name,
        agg.phone,
        agg.email,
        agg.channel,
        agg.created_at,
        agg.last_seen,
        agg.conversation_count,
        COUNT(*) OVER() AS total_count
    FROM agg
    WHERE (
        p_search IS NULL
        OR agg.name  ILIKE '%' || p_search || '%'
        OR agg.phone ILIKE '%' || p_search || '%'
        OR agg.email ILIKE '%' || p_search || '%'
    )
    ORDER BY agg.last_seen DESC
    LIMIT  p_limit                 -- NULL => sem limite (export)
    OFFSET COALESCE(p_offset, 0);
$$;


--
-- Name: rpc_metrics_attendance(uuid, timestamp with time zone, timestamp with time zone); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.rpc_metrics_attendance(p_company_id uuid, p_start timestamp with time zone, p_end timestamp with time zone) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
DECLARE
    v_by_admin jsonb;
    v_first_response_pct numeric;
    v_resolution_pct numeric;
    v_breached_count bigint;
    v_breaches jsonb;
    v_fr_met bigint;
    v_fr_missed bigint;
    v_rr_met bigint;
    v_rr_missed bigint;
    v_rr_breached bigint;
BEGIN
    SELECT COALESCE(
               jsonb_agg(
                   jsonb_build_object(
                       'user_id',  t.user_id,
                       'name',     t.name,
                       'role',     t.role,
                       'is_owner', t.is_owner,
                       'taken',    t.taken,
                       'resolved', t.resolved,
                       'open',     t.open
                   )
                   ORDER BY t.taken DESC
               ),
               '[]'::jsonb
           )
      INTO v_by_admin
      FROM (
          SELECT
              s.human_taken_by_user_id AS user_id,
              NULLIF(trim(both ' ' FROM concat_ws(' ', u.first_name, u.last_name)), '') AS name,
              u.role::text             AS role,
              COALESCE(u.is_owner, false) AS is_owner,
              count(*)                 AS taken,
              count(*) FILTER (WHERE s.resolved_at IS NOT NULL) AS resolved,
              count(*) FILTER (WHERE s.status NOT IN ('resolved', 'closed')) AS open
          FROM public.attendance_sessions s
          LEFT JOIN public.users_v2 u
                 ON u.id = s.human_taken_by_user_id
                AND u.company_id = p_company_id
          WHERE s.company_id = p_company_id
            AND s.human_taken_at >= p_start
            AND s.human_taken_at <  p_end
          GROUP BY s.human_taken_by_user_id, u.first_name, u.last_name, u.role, u.is_owner
      ) t;

    SELECT
        count(*) FILTER (WHERE a.first_response_status = 'met'),
        count(*) FILTER (WHERE a.first_response_status = 'missed'),
        count(*) FILTER (WHERE a.resolution_status = 'met'),
        count(*) FILTER (WHERE a.resolution_status = 'missed'),
        count(*) FILTER (WHERE a.resolution_status = 'breached')
      INTO v_fr_met, v_fr_missed, v_rr_met, v_rr_missed, v_rr_breached
      FROM public.attendance_sla a
      JOIN public.attendance_sessions s
           ON s.id = a.attendance_session_id
          AND s.company_id = p_company_id
     WHERE a.company_id = p_company_id
       AND s.human_taken_at >= p_start
       AND s.human_taken_at <  p_end;

    v_first_response_pct := CASE
        WHEN (v_fr_met + v_fr_missed) = 0 THEN NULL
        ELSE round(v_fr_met::numeric / (v_fr_met + v_fr_missed) * 100, 1)
    END;
    v_resolution_pct := CASE
        WHEN (v_rr_met + v_rr_missed + v_rr_breached) = 0 THEN NULL
        ELSE round(v_rr_met::numeric / (v_rr_met + v_rr_missed + v_rr_breached) * 100, 1)
    END;

    SELECT count(*)
      INTO v_breached_count
      FROM public.sla_events e
     WHERE e.company_id = p_company_id
       AND e.event_type IN ('first_response_missed', 'resolution_missed', 'resolution_breached')
       AND e.created_at >= p_start
       AND e.created_at <  p_end;

    SELECT COALESCE(
               jsonb_agg(
                   jsonb_build_object(
                       'conversation_id', b.conversation_id,
                       'customer',        b.customer,
                       'admin_name',      b.admin_name,
                       'kind',            b.kind,
                       'deadline',        b.deadline,
                       'breached_at',     b.breached_at,
                       'delay_minutes',   b.delay_minutes
                   )
                   ORDER BY b.delay_minutes DESC NULLS LAST
               ),
               '[]'::jsonb
           )
      INTO v_breaches
      FROM (
          SELECT
              e.conversation_id,
              COALESCE(NULLIF(c.user_name, ''), NULLIF(c.user_phone, ''), c.session_id) AS customer,
              NULLIF(trim(both ' ' FROM concat_ws(' ', u.first_name, u.last_name)), '') AS admin_name,
              CASE
                  WHEN e.event_type = 'first_response_missed' THEN 'first_response'
                  ELSE 'resolution'
              END AS kind,
              CASE
                  WHEN e.event_type = 'first_response_missed' THEN a.first_response_deadline
                  ELSE a.resolution_deadline
              END AS deadline,
              e.created_at AS breached_at,
              CASE
                  WHEN (CASE
                            WHEN e.event_type = 'first_response_missed' THEN a.first_response_deadline
                            ELSE a.resolution_deadline
                        END) IS NULL THEN NULL
                  ELSE round(
                           extract(epoch FROM (
                               e.created_at - (CASE
                                   WHEN e.event_type = 'first_response_missed' THEN a.first_response_deadline
                                   ELSE a.resolution_deadline
                               END)
                           )) / 60.0
                       )::int
              END AS delay_minutes
          FROM public.sla_events e
          LEFT JOIN public.attendance_sla a
                 ON a.id = e.attendance_sla_id
                AND a.company_id = p_company_id
          LEFT JOIN public.attendance_sessions s
                 ON s.id = e.attendance_session_id
                AND s.company_id = p_company_id
          LEFT JOIN public.users_v2 u
                 ON u.id = s.human_taken_by_user_id
                AND u.company_id = p_company_id
          LEFT JOIN public.conversations c
                 ON c.id = e.conversation_id
                AND c.company_id = p_company_id
          WHERE e.company_id = p_company_id
            AND e.event_type IN ('first_response_missed', 'resolution_missed', 'resolution_breached')
            AND e.created_at >= p_start
            AND e.created_at <  p_end
          ORDER BY delay_minutes DESC NULLS LAST
          LIMIT 50
      ) b;

    RETURN jsonb_build_object(
        'by_admin', v_by_admin,
        'sla', jsonb_build_object(
            'first_response_pct', v_first_response_pct,
            'resolution_pct',     v_resolution_pct,
            'breached_count',     v_breached_count,
            'breaches',           v_breaches
        )
    );
END;
$$;


--
-- Name: rpc_metrics_by_agent(uuid, timestamp with time zone, timestamp with time zone); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.rpc_metrics_by_agent(p_company_id uuid, p_start timestamp with time zone, p_end timestamp with time zone) RETURNS TABLE(agent_id uuid, agent_name text, messages bigint, conversations bigint)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
BEGIN
    RETURN QUERY
    SELECT
        c.agent_id                        AS agent_id,
        ag.name::text                     AS agent_name,
        count(*)                          AS messages,
        count(DISTINCT m.conversation_id) AS conversations
    FROM public.messages m
    JOIN public.conversations c ON c.id = m.conversation_id
    JOIN public.agents ag       ON ag.id = c.agent_id
                               AND ag.company_id = p_company_id
    WHERE c.company_id = p_company_id
      AND m.role = 'assistant'
      AND m.sender_user_id IS NULL
      AND m.created_at >= p_start
      AND m.created_at <  p_end
    GROUP BY c.agent_id, ag.name
    ORDER BY count(*) DESC;
END;
$$;


--
-- Name: rpc_metrics_summary(uuid, timestamp with time zone, timestamp with time zone); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.rpc_metrics_summary(p_company_id uuid, p_start timestamp with time zone, p_end timestamp with time zone) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
DECLARE
    v_new        bigint;
    v_existing   bigint;
    v_messages   bigint;
    v_leads      bigint;
    v_credits    numeric;
BEGIN
    -- Novas Conversas = created_at no range.
    SELECT count(*) INTO v_new
      FROM public.conversations c
     WHERE c.company_id = p_company_id
       AND c.created_at >= p_start
       AND c.created_at <  p_end;

    -- Conversas Existentes = created_at ANTES do range E last_message_at no range
    -- (SÓ last_message_at, não updated_at — D4).
    SELECT count(*) INTO v_existing
      FROM public.conversations c
     WHERE c.company_id = p_company_id
       AND c.created_at < p_start
       AND c.last_message_at >= p_start
       AND c.last_message_at <  p_end;

    -- Total de Mensagens = COUNT(messages) JOIN conversations (tenant via conversa).
    SELECT count(*) INTO v_messages
      FROM public.messages m
      JOIN public.conversations c ON c.id = m.conversation_id
     WHERE c.company_id = p_company_id
       AND m.created_at >= p_start
       AND m.created_at <  p_end;

    -- Leads Gerados = contatos (mesmo contact_key dos Contatos) COM e-mail OU telefone.
    -- Espelha rpc_list_contacts (20260627_01): contact_key + LEFT JOIN leads/users_v2
    -- escopado por company_id; HAVING exige email não-vazio OU user_phone não-vazio.
    -- Exclui as linhas "—" (sem identificador). Period-scoped por created_at.
    SELECT count(*) INTO v_leads
      FROM (
          SELECT 1
            FROM public.conversations c
            LEFT JOIN public.leads    l ON l.id = c.user_id AND l.company_id = c.company_id
            LEFT JOIN public.users_v2 u ON u.id = c.user_id AND u.company_id = c.company_id
           WHERE c.company_id = p_company_id
             AND c.created_at >= p_start
             AND c.created_at <  p_end
           GROUP BY COALESCE(c.user_id::text, NULLIF(c.user_phone, ''), c.session_id)
          HAVING (MAX(COALESCE(l.email, u.email)) IS NOT NULL
                  AND MAX(COALESCE(l.email, u.email)) <> '')
              OR  MAX(NULLIF(c.user_phone, '')) IS NOT NULL
      ) AS leads_with_contact;

    -- Créditos Consumidos = SUM(ABS(amount_brl)) type='consumption' no range
    -- (mirror billing.py:219-249; amount_brl é negativo p/ débitos).
    SELECT COALESCE(sum(abs(ct.amount_brl)), 0) INTO v_credits
      FROM public.credit_transactions ct
     WHERE ct.company_id = p_company_id
       AND ct.type = 'consumption'
       AND ct.created_at >= p_start
       AND ct.created_at <  p_end;

    RETURN jsonb_build_object(
        'total_conversations',   v_new + v_existing,
        'new_conversations',     v_new,
        'existing_conversations',v_existing,
        'total_messages',        v_messages,
        'leads_generated',       v_leads,
        'credits_consumed_brl',  round(v_credits, 2)
    );
END;
$$;


--
-- Name: rpc_metrics_timeseries(uuid, timestamp with time zone, timestamp with time zone); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.rpc_metrics_timeseries(p_company_id uuid, p_start timestamp with time zone, p_end timestamp with time zone) RETURNS TABLE(date date, conversations bigint, messages bigint)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
BEGIN
    RETURN QUERY
    WITH days AS (
        SELECT generate_series(
                   (p_start AT TIME ZONE 'America/Sao_Paulo')::date,
                   ((p_end AT TIME ZONE 'America/Sao_Paulo') - interval '1 microsecond')::date,
                   interval '1 day'
               )::date AS d
    ),
    conv AS (
        SELECT (c.created_at AT TIME ZONE 'America/Sao_Paulo')::date AS d,
               count(*) AS n
          FROM public.conversations c
         WHERE c.company_id = p_company_id
           AND c.created_at >= p_start
           AND c.created_at <  p_end
         GROUP BY 1
    ),
    msg AS (
        SELECT (m.created_at AT TIME ZONE 'America/Sao_Paulo')::date AS d,
               count(*) AS n
          FROM public.messages m
          JOIN public.conversations c ON c.id = m.conversation_id
         WHERE c.company_id = p_company_id
           AND m.created_at >= p_start
           AND m.created_at <  p_end
         GROUP BY 1
    )
    SELECT days.d AS date,
           COALESCE(conv.n, 0)::bigint AS conversations,
           COALESCE(msg.n,  0)::bigint AS messages
      FROM days
      LEFT JOIN conv ON conv.d = days.d
      LEFT JOIN msg  ON msg.d  = days.d
     ORDER BY days.d;
END;
$$;


--
-- Name: security_audit_admin_users_role(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.security_audit_admin_users_role() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
BEGIN
    PERFORM public.write_security_audit_log(
        'admin_user_role_changed',
        NEW.company_id,
        'admin_users',
        NEW.id,
        'warning',
        jsonb_build_object(
            'previousRole', OLD.role,
            'newRole', NEW.role,
            'dbField', 'admin_users.role'
        )
    );
    RETURN NEW;
END;
$$;


--
-- Name: security_audit_agent_http_tools_url(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.security_audit_agent_http_tools_url() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
DECLARE
    v_action text;
    v_url text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        v_action := 'http_tool_target_url_created';
    ELSE
        v_action := 'http_tool_target_url_updated';
    END IF;

    v_url := COALESCE(NEW.url, '');
    IF btrim(v_url) = '' THEN
        RETURN NEW;
    END IF;

    PERFORM public.write_security_audit_log(
        v_action,
        NEW.company_id,
        'agent_http_tools',
        NEW.id,
        'success',
        jsonb_build_object(
            'targetUrlPresent', true,
            'targetUrlLength', length(v_url),
            'dbField', 'agent_http_tools.target_url',
            'actualColumn', 'agent_http_tools.url'
        )
    );
    RETURN NEW;
END;
$$;


--
-- Name: security_audit_companies_webhook_url(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.security_audit_companies_webhook_url() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
DECLARE
    v_action text;
    v_url text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        v_action := 'company_webhook_url_created';
    ELSE
        v_action := 'company_webhook_url_updated';
    END IF;

    v_url := COALESCE(NEW.webhook_url, '');
    IF btrim(v_url) = '' THEN
        RETURN NEW;
    END IF;

    PERFORM public.write_security_audit_log(
        v_action,
        NEW.id,
        'companies',
        NEW.id,
        'success',
        jsonb_build_object(
            'webhookUrlPresent', true,
            'webhookUrlLength', length(v_url),
            'dbField', 'companies.webhook_url'
        )
    );
    RETURN NEW;
END;
$$;


--
-- Name: security_audit_resource_delete(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.security_audit_resource_delete() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
DECLARE
    v_company_id uuid;
BEGIN
    v_company_id := OLD.company_id;

    PERFORM public.write_security_audit_log(
        'resource_deleted',
        v_company_id,
        TG_TABLE_NAME,
        OLD.id,
        'success',
        jsonb_build_object('deletedResourceType', TG_TABLE_NAME)
    );
    RETURN OLD;
END;
$$;


--
-- Name: security_audit_users_v2_status(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.security_audit_users_v2_status() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
BEGIN
    PERFORM public.write_security_audit_log(
        'user_status_changed',
        NEW.company_id,
        'users_v2',
        NEW.id,
        'success',
        jsonb_build_object(
            'previousStatus', OLD.status,
            'newStatus', NEW.status,
            'dbField', 'users_v2.status'
        )
    );
    RETURN NEW;
END;
$$;


--
-- Name: ucp_connections_touch_config_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.ucp_connections_touch_config_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'public'
    AS $$
BEGIN
    IF (
        OLD.is_active IS DISTINCT FROM NEW.is_active
        OR OLD.store_url IS DISTINCT FROM NEW.store_url
        OR OLD.manifest_version IS DISTINCT FROM NEW.manifest_version
        OR OLD.preferred_transport IS DISTINCT FROM NEW.preferred_transport
        OR OLD.capabilities_enabled IS DISTINCT FROM NEW.capabilities_enabled
    ) THEN
        NEW.config_updated_at = clock_timestamp();
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: update_agent_delegations_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.update_agent_delegations_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;


--
-- Name: update_documents_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.update_documents_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


--
-- Name: update_ucp_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.update_ucp_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


--
-- Name: update_updated_at_column(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.update_updated_at_column() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;


--
-- Name: write_security_audit_log(text, uuid, text, uuid, text, jsonb); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.write_security_audit_log(p_action text, p_company_id uuid, p_resource_type text, p_resource_id uuid, p_status text DEFAULT 'success'::text, p_details jsonb DEFAULT '{}'::jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
BEGIN
    INSERT INTO public.system_logs (
        company_id,
        action_type,
        resource_type,
        resource_id,
        status,
        details
    ) VALUES (
        p_company_id,
        p_action,
        p_resource_type,
        p_resource_id,
        p_status,
        COALESCE(p_details, '{}'::jsonb) || jsonb_build_object(
            'category', 'security_audit',
            'action', p_action,
            'targetId', p_resource_id,
            'targetCompanyId', p_company_id,
            'source', 'db_trigger'
        )
    );
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: app_runtime_secrets; Type: TABLE; Schema: private; Owner: -
--

CREATE TABLE private.app_runtime_secrets (
    name text NOT NULL,
    secret text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT app_runtime_secrets_secret_check CHECK ((length(secret) >= 32))
);


--
-- Name: admin_users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.admin_users (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    email character varying(255) NOT NULL,
    password_hash text NOT NULL,
    name character varying(255) NOT NULL,
    created_at timestamp without time zone DEFAULT now(),
    reset_token text,
    reset_token_expires_at timestamp with time zone,
    password_migrated_at timestamp with time zone,
    reset_attempts integer DEFAULT 0,
    role text DEFAULT 'company_admin'::text NOT NULL,
    company_id uuid,
    failed_login_attempts integer DEFAULT 0,
    account_locked_until timestamp without time zone
);


--
-- Name: COLUMN admin_users.role; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.admin_users.role IS 'Security role for admin sessions. Defaults to company_admin; no implicit master fallback.';


--
-- Name: COLUMN admin_users.company_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.admin_users.company_id IS 'Tenant scope for company_admin records. master_admin access is controlled by JWT role claims.';


--
-- Name: agent_attendance_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_attendance_settings (
    agent_id uuid NOT NULL,
    company_id uuid NOT NULL,
    handoff_enabled boolean DEFAULT false NOT NULL,
    auto_close_enabled boolean DEFAULT false NOT NULL,
    auto_close_after_minutes integer DEFAULT 240 NOT NULL,
    auto_close_scope text DEFAULT 'all_attendance'::text NOT NULL,
    auto_close_message_enabled boolean DEFAULT true NOT NULL,
    auto_close_message text DEFAULT 'Encerramos este atendimento por falta de resposta. Se precisar, é só chamar novamente.'::text NOT NULL,
    reopen_on_customer_reply boolean DEFAULT true NOT NULL,
    agent_can_close boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT agent_attendance_after_minutes_check CHECK ((auto_close_after_minutes >= 5)),
    CONSTRAINT agent_attendance_message_check CHECK (((auto_close_message_enabled = false) OR (length(btrim(auto_close_message)) > 0))),
    CONSTRAINT agent_attendance_settings_auto_close_scope_check CHECK ((auto_close_scope = ANY (ARRAY['all_attendance'::text, 'human_only'::text])))
);


--
-- Name: agent_delegations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_delegations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    orchestrator_id uuid NOT NULL,
    subagent_id uuid NOT NULL,
    task_description text NOT NULL,
    is_active boolean DEFAULT true,
    max_context_chars integer DEFAULT 2000,
    timeout_seconds integer DEFAULT 30,
    max_iterations integer DEFAULT 5,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    company_id uuid,
    CONSTRAINT no_self_delegation CHECK ((orchestrator_id <> subagent_id))
);


--
-- Name: COLUMN agent_delegations.company_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_delegations.company_id IS 'Tenant scope for delegation RLS; backfilled from orchestrator agent.';


--
-- Name: agent_http_tools; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_http_tools (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_id uuid NOT NULL,
    name character varying(64) NOT NULL,
    description text NOT NULL,
    method character varying(10) DEFAULT 'GET'::character varying,
    url text NOT NULL,
    headers jsonb DEFAULT '{}'::jsonb,
    parameters jsonb DEFAULT '[]'::jsonb,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    body_template jsonb,
    company_id uuid
);


--
-- Name: COLUMN agent_http_tools.company_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_http_tools.company_id IS 'Tenant scope for HTTP tool RLS; backfilled from the owning agent.';


--
-- Name: agent_mcp_connections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_mcp_connections (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_id uuid NOT NULL,
    mcp_server_id uuid NOT NULL,
    access_token text,
    refresh_token text,
    token_expires_at timestamp with time zone,
    scopes_granted jsonb,
    is_active boolean DEFAULT true,
    connected_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    company_id uuid,
    config_updated_at timestamp with time zone DEFAULT now() NOT NULL,
    connection_config jsonb DEFAULT '{}'::jsonb,
    connection_metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: COLUMN agent_mcp_connections.company_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_mcp_connections.company_id IS 'Tenant scope for MCP connection RLS; backfilled from the owning agent.';


--
-- Name: COLUMN agent_mcp_connections.config_updated_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_mcp_connections.config_updated_at IS 'Timestamp of the last configuration change, used by the Tool Registry cache fingerprint. OAuth token columns (access_token, refresh_token, token_expires_at) do not touch it.';


--
-- Name: COLUMN agent_mcp_connections.connection_config; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_mcp_connections.connection_config IS 'Config por conexao consumida na montagem da URL/chamada remota (ex.: {"project_ref": "abc"} no Supabase). Mudanca avanca config_updated_at via trigger.';


--
-- Name: COLUMN agent_mcp_connections.connection_metadata; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_mcp_connections.connection_metadata IS 'Identidade NAO sensivel da conta/workspace conectada (ex.: workspace_name/workspace_id do Notion), exibida na UI. Nunca tokens.';


--
-- Name: agent_mcp_tools; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_mcp_tools (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_id uuid NOT NULL,
    mcp_server_id uuid NOT NULL,
    mcp_server_name character varying(100) NOT NULL,
    tool_name character varying(100) NOT NULL,
    variable_name character varying(150) NOT NULL,
    description text,
    input_schema jsonb,
    is_enabled boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    company_id uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    is_available boolean DEFAULT true NOT NULL
);


--
-- Name: COLUMN agent_mcp_tools.company_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_mcp_tools.company_id IS 'Tenant scope for MCP tool RLS; backfilled from the owning agent.';


--
-- Name: COLUMN agent_mcp_tools.updated_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_mcp_tools.updated_at IS 'Last mutation timestamp, used by the Tool Registry cache fingerprint.';


--
-- Name: COLUMN agent_mcp_tools.is_available; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_mcp_tools.is_available IS 'false = tool ausente do ultimo tools/list do servidor. Nao deletamos para preservar a curadoria (is_enabled).';


--
-- Name: agents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agents (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name character varying(255) NOT NULL,
    slug character varying(255) NOT NULL,
    is_active boolean DEFAULT true,
    llm_provider character varying(50),
    llm_model character varying(100),
    llm_api_key text,
    llm_temperature numeric(3,2) DEFAULT 0.7,
    llm_max_tokens integer DEFAULT 2000,
    llm_top_p numeric(3,2) DEFAULT 1.0,
    llm_top_k integer DEFAULT 40,
    llm_frequency_penalty numeric(3,2) DEFAULT 0.0,
    llm_presence_penalty numeric(3,2) DEFAULT 0.0,
    agent_system_prompt text,
    agent_enabled boolean DEFAULT true,
    use_langchain boolean DEFAULT false,
    allow_web_search boolean DEFAULT true,
    allow_vision boolean DEFAULT false,
    vision_model text,
    vision_api_key text,
    tools_config jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    avatar_url text,
    reasoning_effort character varying(10) DEFAULT 'medium'::character varying,
    verbosity character varying(10) DEFAULT 'medium'::character varying,
    is_hyde_enabled boolean DEFAULT false,
    widget_config jsonb DEFAULT '{}'::jsonb,
    security_settings jsonb DEFAULT '{"enabled": false, "check_nsfw": true, "check_urls": false, "pii_action": "mask", "custom_regex": [], "error_message": "Sua mensagem viola as políticas de segurança.", "url_whitelist": [], "allowed_topics": [], "check_jailbreak": true, "check_secret_keys": true}'::jsonb,
    is_subagent boolean DEFAULT false,
    allow_direct_chat boolean DEFAULT false,
    retrieval_mode text DEFAULT 'semantic'::text NOT NULL,
    thinking_enabled boolean DEFAULT false,
    CONSTRAINT check_retrieval_mode CHECK ((retrieval_mode = ANY (ARRAY['semantic'::text, 'filesystem'::text]))),
    CONSTRAINT chk_reasoning_effort CHECK (((reasoning_effort IS NULL) OR ((reasoning_effort)::text = ANY (ARRAY[('none'::character varying)::text, ('low'::character varying)::text, ('medium'::character varying)::text, ('high'::character varying)::text])))),
    CONSTRAINT chk_verbosity CHECK (((verbosity IS NULL) OR ((verbosity)::text = ANY (ARRAY[('low'::character varying)::text, ('medium'::character varying)::text, ('high'::character varying)::text]))))
);


--
-- Name: COLUMN agents.is_subagent; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agents.is_subagent IS 'Se true, esconde widget/WhatsApp/canais públicos no frontend';


--
-- Name: COLUMN agents.allow_direct_chat; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agents.allow_direct_chat IS 'Se true, subagent aparece no chat test para o admin treinar/debugar';


--
-- Name: attendance_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.attendance_sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    conversation_id uuid NOT NULL,
    company_id uuid NOT NULL,
    agent_id uuid,
    team_id uuid,
    user_id uuid,
    channel text DEFAULT 'web'::text NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    ai_started_at timestamp with time zone,
    human_requested_at timestamp with time zone,
    human_requested_by_type text,
    human_requested_by_agent_id uuid,
    human_requested_by_user_id uuid,
    human_request_reason text,
    human_taken_at timestamp with time zone,
    human_taken_by_user_id uuid,
    first_human_response_at timestamp with time zone,
    returned_to_ai_at timestamp with time zone,
    returned_to_ai_by_user_id uuid,
    resolved_at timestamp with time zone,
    closed_at timestamp with time zone,
    closed_by_type text,
    closed_by_user_id uuid,
    closed_by_agent_id uuid,
    close_reason text,
    close_summary text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT attendance_sessions_closed_by_type_check CHECK (((closed_by_type IS NULL) OR (closed_by_type = ANY (ARRAY['human'::text, 'agent'::text, 'system'::text])))),
    CONSTRAINT attendance_sessions_human_requested_by_type_check CHECK (((human_requested_by_type IS NULL) OR (human_requested_by_type = ANY (ARRAY['agent'::text, 'human'::text, 'system'::text])))),
    CONSTRAINT attendance_sessions_status_check CHECK ((status = ANY (ARRAY['open'::text, 'human_requested'::text, 'human_active'::text, 'pending_customer'::text, 'returned_to_ai'::text, 'resolved'::text, 'closed'::text])))
);


--
-- Name: attendance_sla; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.attendance_sla (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    attendance_session_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    company_id uuid NOT NULL,
    policy_id uuid,
    sla_level text NOT NULL,
    health_status text DEFAULT 'within_sla'::text NOT NULL,
    first_response_status text DEFAULT 'pending'::text NOT NULL,
    resolution_status text DEFAULT 'pending'::text NOT NULL,
    started_at timestamp with time zone NOT NULL,
    first_response_deadline timestamp with time zone NOT NULL,
    first_response_at timestamp with time zone,
    resolution_deadline timestamp with time zone NOT NULL,
    resolved_at timestamp with time zone,
    paused_at timestamp with time zone,
    paused_duration_seconds integer DEFAULT 0 NOT NULL,
    policy_snapshot jsonb NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT attendance_sla_first_response_status_check CHECK ((first_response_status = ANY (ARRAY['pending'::text, 'met'::text, 'missed'::text]))),
    CONSTRAINT attendance_sla_health_status_check CHECK ((health_status = ANY (ARRAY['within_sla'::text, 'at_risk'::text, 'critical'::text, 'breached'::text, 'paused'::text]))),
    CONSTRAINT attendance_sla_resolution_status_check CHECK ((resolution_status = ANY (ARRAY['pending'::text, 'met'::text, 'missed'::text, 'breached'::text]))),
    CONSTRAINT attendance_sla_sla_level_check CHECK ((sla_level = ANY (ARRAY['normal'::text, 'high'::text, 'critical'::text])))
);


--
-- Name: checkpoint_blobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checkpoint_blobs (
    thread_id text NOT NULL,
    checkpoint_ns text DEFAULT ''::text NOT NULL,
    channel text NOT NULL,
    version text NOT NULL,
    type text NOT NULL,
    blob bytea
);


--
-- Name: checkpoint_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checkpoint_migrations (
    v integer NOT NULL
);


--
-- Name: checkpoint_writes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checkpoint_writes (
    thread_id text NOT NULL,
    checkpoint_ns text DEFAULT ''::text NOT NULL,
    checkpoint_id text NOT NULL,
    task_id text NOT NULL,
    idx integer NOT NULL,
    channel text NOT NULL,
    type text,
    blob bytea NOT NULL,
    task_path text DEFAULT ''::text NOT NULL
);


--
-- Name: checkpoints; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checkpoints (
    thread_id text NOT NULL,
    checkpoint_ns text DEFAULT ''::text NOT NULL,
    checkpoint_id text NOT NULL,
    parent_checkpoint_id text,
    type text,
    checkpoint jsonb NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    company_id uuid
);


--
-- Name: COLUMN checkpoints.company_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.checkpoints.company_id IS 'Tenant scope for LangGraph checkpoint RLS; nullable for legacy rows until application backfill.';


--
-- Name: companies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.companies (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_name character varying(255) NOT NULL,
    legal_name character varying(255),
    cnpj character varying(18),
    webhook_url text,
    n8n_instance_url text,
    plan_type character varying(50) DEFAULT 'starter'::character varying,
    monthly_fee numeric(10,2) DEFAULT 0,
    setup_fee numeric(10,2) DEFAULT 0,
    max_users integer DEFAULT 5,
    status character varying(20) DEFAULT 'active'::character varying,
    primary_contact_name character varying(255),
    primary_contact_email character varying(255),
    primary_contact_phone character varying(20),
    notes text,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now(),
    use_langchain boolean DEFAULT false,
    agent_enabled boolean DEFAULT false,
    llm_provider character varying(50),
    llm_model character varying(100),
    llm_api_key text,
    llm_temperature numeric(3,2) DEFAULT 0.7,
    llm_max_tokens integer DEFAULT 2000,
    llm_top_p numeric(3,2) DEFAULT 1.0,
    llm_top_k integer,
    llm_frequency_penalty numeric(3,2),
    llm_presence_penalty numeric(3,2),
    agent_system_prompt text,
    agent_user_prompt_template text,
    agent_config_updated_at timestamp with time zone,
    agent_config_updated_by uuid,
    allow_web_search boolean DEFAULT true,
    allow_vision boolean DEFAULT false,
    vision_api_key text,
    vision_model text,
    cep character varying(9),
    street character varying(255),
    number character varying(50),
    complement character varying(255),
    neighborhood character varying(100),
    city character varying(100),
    state character varying(2),
    CONSTRAINT companies_status_check CHECK (((status)::text = ANY (ARRAY[('active'::character varying)::text, ('trial'::character varying)::text, ('suspended'::character varying)::text, ('cancelled'::character varying)::text])))
);


--
-- Name: company_attendance_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.company_attendance_settings (
    company_id uuid NOT NULL,
    auto_close_enabled boolean DEFAULT false NOT NULL,
    auto_close_after_minutes integer DEFAULT 240 NOT NULL,
    auto_close_scope text DEFAULT 'all_attendance'::text NOT NULL,
    auto_close_message_enabled boolean DEFAULT true NOT NULL,
    auto_close_message text DEFAULT 'Encerramos este atendimento por falta de resposta. Se precisar, é só chamar novamente.'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT company_attendance_message_check CHECK (((auto_close_message_enabled = false) OR (length(btrim(auto_close_message)) > 0))),
    CONSTRAINT company_attendance_settings_auto_close_after_minutes_check CHECK ((auto_close_after_minutes >= 5)),
    CONSTRAINT company_attendance_settings_auto_close_scope_check CHECK ((auto_close_scope = ANY (ARRAY['all_attendance'::text, 'human_only'::text])))
);


--
-- Name: company_credits; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.company_credits (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid,
    balance_brl numeric(10,4) DEFAULT 0,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    alert_80_sent boolean DEFAULT false,
    alert_100_sent boolean DEFAULT false
);


--
-- Name: conversation_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.conversation_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    conversation_id uuid NOT NULL,
    attendance_session_id uuid,
    company_id uuid NOT NULL,
    agent_id uuid,
    event_type text NOT NULL,
    actor_type text,
    actor_user_id uuid,
    actor_agent_id uuid,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    idempotency_key text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT conversation_events_actor_type_check CHECK (((actor_type IS NULL) OR (actor_type = ANY (ARRAY['customer'::text, 'agent'::text, 'human'::text, 'system'::text])))),
    CONSTRAINT conversation_events_event_type_check CHECK ((event_type = ANY (ARRAY['attendance_started'::text, 'ai_message_sent'::text, 'customer_message_received'::text, 'handoff_requested'::text, 'handoff_notified'::text, 'human_claimed'::text, 'human_message_sent'::text, 'returned_to_ai'::text, 'resolved_by_human'::text, 'resolved_by_agent'::text, 'closed_by_human'::text, 'closed_by_agent'::text, 'closed_by_system'::text, 'auto_close_scheduled'::text, 'auto_close_cancelled'::text, 'timeout_closed'::text, 'reopened_by_customer'::text, 'reopened_by_admin'::text, 'note_added'::text])))
);


--
-- Name: conversation_inactivity_timers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.conversation_inactivity_timers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    conversation_id uuid NOT NULL,
    attendance_session_id uuid,
    company_id uuid NOT NULL,
    agent_id uuid,
    timer_type text DEFAULT 'auto_close'::text NOT NULL,
    status text DEFAULT 'scheduled'::text NOT NULL,
    basis_message_id uuid,
    basis_at timestamp with time zone NOT NULL,
    next_action_at timestamp with time zone NOT NULL,
    executed_at timestamp with time zone,
    cancelled_at timestamp with time zone,
    error_message text,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT conversation_inactivity_timers_status_check CHECK ((status = ANY (ARRAY['scheduled'::text, 'processing'::text, 'cancelled'::text, 'executed'::text, 'failed'::text]))),
    CONSTRAINT conversation_inactivity_timers_timer_type_check CHECK ((timer_type = 'auto_close'::text))
);


--
-- Name: conversation_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.conversation_logs (
    id uuid DEFAULT extensions.uuid_generate_v4() NOT NULL,
    "timestamp" timestamp with time zone DEFAULT now(),
    company_id uuid NOT NULL,
    user_id uuid NOT NULL,
    session_id text NOT NULL,
    user_question text NOT NULL,
    assistant_response text NOT NULL,
    llm_provider text NOT NULL,
    llm_model text NOT NULL,
    llm_temperature double precision NOT NULL,
    tokens_input integer,
    tokens_output integer,
    tokens_total integer,
    rag_chunks jsonb,
    rag_chunks_count integer DEFAULT 0,
    response_time_ms integer,
    rag_search_time_ms integer,
    status text DEFAULT 'success'::text,
    error_message text,
    created_at timestamp with time zone DEFAULT now(),
    search_strategy text,
    retrieval_score double precision,
    agent_id uuid,
    internal_steps jsonb
);


--
-- Name: COLUMN conversation_logs.internal_steps; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.conversation_logs.internal_steps IS 'Traces de execução de SubAgents (ReAct loop steps, tokens, latência)';


--
-- Name: conversations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.conversations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    session_id text NOT NULL,
    title text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    company_id uuid,
    status character varying(20) DEFAULT 'open'::character varying,
    channel character varying(20) DEFAULT 'web'::character varying,
    last_message_preview text,
    unread_count integer DEFAULT 0,
    agent_name text DEFAULT 'Smith Agent'::text,
    status_color character varying(20) DEFAULT 'green'::character varying,
    user_name text,
    user_avatar text,
    user_phone text,
    last_message_at timestamp with time zone DEFAULT now(),
    agent_id uuid,
    human_handoff_reason text,
    assigned_user_id uuid,
    team_id uuid,
    current_attendance_session_id uuid,
    human_requested_at timestamp with time zone,
    human_taken_at timestamp with time zone,
    first_human_response_at timestamp with time zone,
    returned_to_ai_at timestamp with time zone,
    resolved_at timestamp with time zone,
    closed_at timestamp with time zone,
    closed_by_type text,
    closed_by_user_id uuid,
    closed_by_agent_id uuid,
    close_reason text,
    close_summary text,
    last_customer_message_at timestamp with time zone,
    last_ai_message_at timestamp with time zone,
    last_human_message_at timestamp with time zone,
    customer_waiting_since timestamp with time zone,
    agent_paused boolean DEFAULT false NOT NULL,
    agent_paused_reason text,
    sla_priority text,
    CONSTRAINT conversations_sla_priority_check CHECK (((sla_priority IS NULL) OR (sla_priority = ANY (ARRAY['normal'::text, 'high'::text, 'critical'::text])))),
    CONSTRAINT conversations_status_check CHECK (((status)::text = ANY ((ARRAY['open'::character varying, 'HUMAN_REQUESTED'::character varying, 'HUMAN_ACTIVE'::character varying, 'PENDING_CUSTOMER'::character varying, 'RETURNED_TO_AI'::character varying, 'RESOLVED'::character varying, 'CLOSED'::character varying])::text[])))
);


--
-- Name: credit_transactions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.credit_transactions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid,
    agent_id uuid,
    type character varying(20) NOT NULL,
    amount_brl numeric(10,4) NOT NULL,
    balance_after numeric(10,4),
    model_name character varying(100),
    tokens_input integer,
    tokens_output integer,
    description text,
    stripe_payment_id character varying(100),
    created_at timestamp with time zone DEFAULT now(),
    idempotency_key text,
    CONSTRAINT credit_transactions_type_check CHECK (((type)::text = ANY (ARRAY[('subscription'::character varying)::text, ('topup'::character varying)::text, ('consumption'::character varying)::text, ('refund'::character varying)::text, ('bonus'::character varying)::text])))
);


--
-- Name: documents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.documents (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    file_name text NOT NULL,
    file_type text NOT NULL,
    file_size integer NOT NULL,
    minio_path text NOT NULL,
    qdrant_collection text,
    status text DEFAULT 'pending'::text NOT NULL,
    error_message text,
    chunks_count integer DEFAULT 0,
    processed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    ingestion_strategy character varying(50),
    quality_score double precision,
    quality_audited_at timestamp with time zone,
    agent_id uuid,
    ingestion_mode text DEFAULT 'semantic'::text NOT NULL,
    fs_storage_path text,
    fs_token_count integer,
    fs_section_count integer,
    fs_outline jsonb,
    CONSTRAINT check_ingestion_mode CHECK ((ingestion_mode = ANY (ARRAY['semantic'::text, 'filesystem'::text]))),
    CONSTRAINT check_ingestion_strategy CHECK (((ingestion_strategy IS NULL) OR ((ingestion_strategy)::text = ANY (ARRAY['recursive'::text, 'semantic'::text, 'page'::text, 'agentic'::text, 'csv'::text])))),
    CONSTRAINT documents_chunks_count_check CHECK ((chunks_count >= 0)),
    CONSTRAINT documents_file_size_check CHECK ((file_size > 0)),
    CONSTRAINT documents_file_type_check CHECK ((file_type = ANY (ARRAY['pdf'::text, 'docx'::text, 'txt'::text, 'md'::text, 'csv'::text]))),
    CONSTRAINT documents_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'processing'::text, 'completed'::text, 'failed'::text])))
);


--
-- Name: handoff_notification_recipients; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.handoff_notification_recipients (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    agent_id uuid,
    channel text NOT NULL,
    recipient_value text NOT NULL,
    recipient_normalized text NOT NULL,
    display_name text,
    enabled boolean DEFAULT true NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT handoff_notification_recipients_channel_check CHECK ((channel = ANY (ARRAY['email'::text, 'whatsapp'::text])))
);


--
-- Name: integrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.integrations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid,
    provider character varying(50) DEFAULT 'z-api'::character varying,
    identifier character varying(100) NOT NULL,
    token text NOT NULL,
    instance_id text,
    base_url text DEFAULT 'https://api.z-api.io/instances'::text,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT timezone('utc'::text, now()),
    client_token text,
    buffer_enabled boolean DEFAULT true,
    buffer_debounce_seconds integer DEFAULT 3,
    buffer_max_wait_seconds integer DEFAULT 10,
    agent_id uuid,
    updated_at timestamp with time zone DEFAULT now(),
    webhook_token text,
    webhook_token_hash text,
    webhook_token_prefix text,
    webhook_token_rotated_at timestamp with time zone
);


--
-- Name: COLUMN integrations.webhook_token; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.integrations.webhook_token IS 'Token de webhook em TEXTO PURO, mantido apenas para RE-EXIBIÇÃO da URL no GET admin (o cliente re-copia a URL quando quiser). NUNCA é lido no caminho inbound (que casa por hash) e NUNCA vai para log/Sentry/audit. Verify-only (inbound) — NÃO confundir com `token`/`client_token`, que são credenciais de ENVIO (outbound) e precisam continuar replayáveis.';


--
-- Name: COLUMN integrations.webhook_token_hash; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.integrations.webhook_token_hash IS 'SHA-256 hex(64) do token completo. Credencial BEARER inbound por-integração = autenticação + chave de roteamento de tenant. Chave de lookup O(1) do inbound (índice UNIQUE parcial). A borda casa por este hash (hmac.compare_digest), nunca pelo `connectedPhone` do corpo — é o que fecha a forja cross-tenant.';


--
-- Name: COLUMN integrations.webhook_token_prefix; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.integrations.webhook_token_prefix IS 'Primeiros 12 chars do token (ex. ''wh_zapi_aB3d''). NÃO-SECRETO — usado em UI/audit/log (observabilidade/grep) no lugar do token cru. Não revela entropia.';


--
-- Name: COLUMN integrations.webhook_token_rotated_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.integrations.webhook_token_rotated_at IS 'Timestamp da última geração/rotação do token (endpoint de regeneração). NULL até o token ser gerado.';


--
-- Name: internal_whatsapp_blocklist; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.internal_whatsapp_blocklist (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    agent_id uuid,
    integration_id uuid,
    phone_normalized text NOT NULL,
    source_recipient_id uuid,
    reason text DEFAULT 'handoff_notification_recipient'::text NOT NULL,
    active boolean DEFAULT true NOT NULL,
    block_count integer DEFAULT 0 NOT NULL,
    last_blocked_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: invites; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.invites (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    token text NOT NULL,
    created_by uuid,
    email_restriction text,
    max_uses integer DEFAULT 1000,
    current_uses integer DEFAULT 0,
    expires_at timestamp without time zone,
    created_at timestamp without time zone DEFAULT now(),
    role character varying(50) DEFAULT 'member'::character varying,
    email character varying(255),
    name character varying(255),
    is_owner_invite boolean DEFAULT false
);


--
-- Name: leads; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.leads (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    email text NOT NULL,
    name text,
    phone text,
    custom_fields jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now(),
    last_seen_at timestamp with time zone DEFAULT now()
);


--
-- Name: legal_documents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.legal_documents (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    type character varying(50) NOT NULL,
    title character varying(255) NOT NULL,
    content text NOT NULL,
    version character varying(20) NOT NULL,
    is_active boolean DEFAULT false,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT legal_documents_type_check CHECK (((type)::text = ANY ((ARRAY['terms_of_use'::character varying, 'privacy_policy'::character varying])::text[])))
);


--
-- Name: llm_pricing; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_pricing (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    model_name character varying(100) NOT NULL,
    input_price_per_million numeric(10,4) NOT NULL,
    output_price_per_million numeric(10,4) NOT NULL,
    unit character varying(20) DEFAULT 'token'::character varying,
    is_active boolean DEFAULT true,
    provider character varying(50),
    display_name character varying(100),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    sell_multiplier numeric(4,2) DEFAULT 2.68,
    cache_write_multiplier numeric(5,2) DEFAULT 1.25,
    cache_read_multiplier numeric(5,2) DEFAULT 0.10,
    cached_input_multiplier numeric(5,2) DEFAULT 0.50,
    selectable boolean DEFAULT true,
    tier character varying(20),
    is_recommended boolean DEFAULT false,
    supports_temperature boolean DEFAULT true,
    supports_reasoning_effort boolean DEFAULT false,
    supports_thinking boolean DEFAULT false,
    thinking_api character varying(20),
    supports_vision boolean DEFAULT false,
    supports_tools boolean DEFAULT true,
    supports_verbosity boolean DEFAULT false
);


--
-- Name: mcp_oauth_clients; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mcp_oauth_clients (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    mcp_server_id uuid NOT NULL,
    client_id text NOT NULL,
    client_secret text,
    registration_access_token text,
    registration_client_uri text,
    auth_metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: TABLE mcp_oauth_clients; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.mcp_oauth_clients IS 'Registro OAuth (DCR RFC 7591) do Agent Smith junto a cada MCP server remoto. Secrets criptografados pelo backend (encryption_service). Acesso apenas via service role.';


--
-- Name: mcp_servers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mcp_servers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name character varying(100) NOT NULL,
    display_name character varying(255) NOT NULL,
    description text,
    package_name character varying(255) NOT NULL,
    command jsonb NOT NULL,
    oauth_provider character varying(50),
    oauth_scopes jsonb,
    env_vars jsonb,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    server_type character varying(20) DEFAULT 'internal'::character varying NOT NULL,
    url text,
    extra_headers jsonb DEFAULT '{}'::jsonb,
    CONSTRAINT mcp_servers_remote_url_check CHECK ((((server_type)::text <> 'remote'::text) OR (url IS NOT NULL))),
    CONSTRAINT mcp_servers_server_type_check CHECK (((server_type)::text = ANY ((ARRAY['internal'::character varying, 'remote'::character varying])::text[])))
);


--
-- Name: COLUMN mcp_servers.server_type; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.mcp_servers.server_type IS 'internal = subprocess stdio (SUP-MCP-020) | remote = Streamable HTTP oficial do provider.';


--
-- Name: COLUMN mcp_servers.url; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.mcp_servers.url IS 'Endpoint do MCP remoto (https obrigatorio; validado tambem no backend). NULL para internos.';


--
-- Name: COLUMN mcp_servers.extra_headers; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.mcp_servers.extra_headers IS 'Headers fixos adicionais enviados em toda chamada ao servidor remoto.';


--
-- Name: memory_processing_locks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_processing_locks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    session_id text NOT NULL,
    company_id uuid NOT NULL,
    is_processing boolean DEFAULT false,
    last_trigger_at timestamp with time zone,
    last_completed_at timestamp with time zone,
    last_message_count integer DEFAULT 0,
    scheduled_for timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    agent_id uuid
);


--
-- Name: memory_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_settings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid,
    web_summarization_mode text DEFAULT 'session_end'::text,
    web_message_threshold integer DEFAULT 20,
    web_inactivity_timeout_min integer DEFAULT 30,
    whatsapp_summarization_mode text DEFAULT 'message_count'::text,
    whatsapp_sliding_window_size integer DEFAULT 50,
    whatsapp_time_interval_hours integer DEFAULT 24,
    whatsapp_message_threshold integer DEFAULT 50,
    extract_user_profile boolean DEFAULT true,
    extract_session_summary boolean DEFAULT true,
    memory_llm_model text DEFAULT 'gpt-4o-mini'::text,
    debounce_seconds integer DEFAULT 10,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    agent_id uuid
);


--
-- Name: messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.messages (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    conversation_id uuid NOT NULL,
    role text NOT NULL,
    content text NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    type text DEFAULT 'text'::text,
    audio_url text,
    image_url text,
    sender_user_id uuid,
    author_type text,
    is_system boolean DEFAULT false NOT NULL,
    company_id uuid,
    CONSTRAINT messages_author_type_check CHECK (((author_type IS NULL) OR (author_type = ANY (ARRAY['customer'::text, 'ai_agent'::text, 'human_operator'::text, 'system'::text])))),
    CONSTRAINT messages_role_check CHECK ((role = ANY (ARRAY['user'::text, 'assistant'::text]))),
    CONSTRAINT messages_type_check CHECK ((type = ANY (ARRAY['text'::text, 'voice'::text])))
);


--
-- Name: notification_deliveries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_deliveries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    conversation_id uuid,
    attendance_session_id uuid,
    recipient_id uuid,
    event_type text NOT NULL,
    idempotency_key text NOT NULL,
    channel text NOT NULL,
    recipient_value text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone,
    last_attempt_at timestamp with time zone,
    locked_until timestamp with time zone,
    locked_by text,
    provider_message_id text,
    last_error text,
    sent_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT notification_deliveries_channel_check CHECK ((channel = ANY (ARRAY['email'::text, 'whatsapp'::text]))),
    CONSTRAINT notification_deliveries_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'sent'::text, 'failed'::text, 'skipped'::text])))
);


--
-- Name: password_reset_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.password_reset_tokens (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid,
    token character varying(255) NOT NULL,
    expires_at timestamp without time zone NOT NULL,
    used_at timestamp without time zone,
    created_at timestamp without time zone DEFAULT now()
);


--
-- Name: payment_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payment_history (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid,
    amount numeric(10,2) NOT NULL,
    currency character varying(3) DEFAULT 'BRL'::character varying,
    status character varying(20) NOT NULL,
    payment_method character varying(50),
    stripe_payment_intent_id character varying(255),
    stripe_invoice_id character varying(255),
    created_at timestamp without time zone DEFAULT now(),
    CONSTRAINT payment_history_payment_method_check CHECK (((payment_method)::text = ANY (ARRAY[('credit_card'::character varying)::text, ('pix'::character varying)::text, ('boleto'::character varying)::text]))),
    CONSTRAINT payment_history_status_check CHECK (((status)::text = ANY (ARRAY[('pending'::character varying)::text, ('completed'::character varying)::text, ('failed'::character varying)::text, ('refunded'::character varying)::text])))
);


--
-- Name: plans; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.plans (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name character varying(100) NOT NULL,
    slug character varying(50) NOT NULL,
    description text,
    monthly_price numeric(10,2) NOT NULL,
    yearly_price numeric(10,2),
    credits_limit integer NOT NULL,
    storage_limit_mb integer NOT NULL,
    max_users integer DEFAULT 1,
    features jsonb,
    is_active boolean DEFAULT true,
    sort_order integer DEFAULT 0,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now(),
    price_brl numeric(10,2),
    display_credits integer,
    max_agents integer DEFAULT 3,
    max_knowledge_bases integer DEFAULT 5,
    stripe_product_id character varying(100),
    stripe_price_id character varying(100),
    display_order integer DEFAULT 0
);


--
-- Name: platform_provider_alerts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_provider_alerts (
    provider text NOT NULL,
    kind text DEFAULT 'balance'::text NOT NULL,
    message text,
    detected_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE platform_provider_alerts; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.platform_provider_alerts IS 'Platform-wide LLM provider health alerts (out-of-balance/quota). Master-only: written by the backend service role on a balance error during a chat turn, auto-resolved when a turn for that provider succeeds, read by the master admin banner. NOT tenant-scoped.';


--
-- Name: platform_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_settings (
    key text NOT NULL,
    value text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by uuid,
    CONSTRAINT platform_settings_value_not_empty CHECK ((length(btrim(value)) > 0))
);


--
-- Name: TABLE platform_settings; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.platform_settings IS 'Config global da plataforma (key-value). Acesso SOMENTE via master admin / service-role.';


--
-- Name: sanitization_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sanitization_jobs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    original_filename text NOT NULL,
    original_file_path text NOT NULL,
    original_file_size bigint NOT NULL,
    original_mime_type text NOT NULL,
    sanitized_file_path text,
    sanitized_file_size bigint,
    status text DEFAULT 'pending'::text NOT NULL,
    progress integer DEFAULT 0 NOT NULL,
    error_message text,
    pages_count integer,
    images_count integer,
    tables_count integer,
    processing_time_seconds real,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone DEFAULT (now() + '7 days'::interval) NOT NULL,
    extract_images boolean DEFAULT false NOT NULL
);


--
-- Name: COLUMN sanitization_jobs.extract_images; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.sanitization_jobs.extract_images IS 'Se true, ativa Vision API para descrever imagens durante a sanitização';


--
-- Name: session_summaries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.session_summaries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    session_id text NOT NULL,
    user_id uuid NOT NULL,
    company_id uuid NOT NULL,
    summary text NOT NULL,
    channel text DEFAULT 'web'::text,
    messages_count integer DEFAULT 0,
    started_at timestamp with time zone,
    ended_at timestamp with time zone,
    topics text[] DEFAULT '{}'::text[],
    decisions text[] DEFAULT '{}'::text[],
    pending_items text[] DEFAULT '{}'::text[],
    created_at timestamp with time zone DEFAULT now(),
    agent_id uuid
);


--
-- Name: sla_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sla_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    attendance_sla_id uuid,
    attendance_session_id uuid,
    conversation_id uuid NOT NULL,
    company_id uuid NOT NULL,
    event_type text NOT NULL,
    actor_type text,
    actor_user_id uuid,
    actor_agent_id uuid,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT sla_events_actor_type_check CHECK (((actor_type IS NULL) OR (actor_type = ANY (ARRAY['agent'::text, 'human'::text, 'system'::text])))),
    CONSTRAINT sla_events_event_type_check CHECK ((event_type = ANY (ARRAY['sla_started'::text, 'first_response_met'::text, 'first_response_missed'::text, 'at_risk_50pct'::text, 'critical_75pct'::text, 'resolution_breached'::text, 'resolution_met'::text, 'resolution_missed'::text, 'sla_paused'::text, 'sla_resumed'::text])))
);


--
-- Name: sla_policies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sla_policies (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid NOT NULL,
    name text DEFAULT 'Política padrão'::text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    timezone text DEFAULT 'America/Sao_Paulo'::text NOT NULL,
    business_hours_enabled boolean DEFAULT false NOT NULL,
    working_days integer[] DEFAULT ARRAY[1, 2, 3, 4, 5] NOT NULL,
    working_start time without time zone,
    working_end time without time zone,
    normal_first_response_minutes integer DEFAULT 15 NOT NULL,
    normal_resolution_minutes integer DEFAULT 240 NOT NULL,
    high_first_response_minutes integer DEFAULT 5 NOT NULL,
    high_resolution_minutes integer DEFAULT 120 NOT NULL,
    critical_first_response_minutes integer DEFAULT 2 NOT NULL,
    critical_resolution_minutes integer DEFAULT 60 NOT NULL,
    default_sla_level text DEFAULT 'normal'::text NOT NULL,
    created_by uuid,
    updated_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT sla_policies_business_hours_check CHECK (((business_hours_enabled = false) OR ((working_start IS NOT NULL) AND (working_end IS NOT NULL) AND (working_start < working_end)))),
    CONSTRAINT sla_policies_default_sla_level_check CHECK ((default_sla_level = ANY (ARRAY['normal'::text, 'high'::text, 'critical'::text]))),
    CONSTRAINT sla_policies_minutes_positive_check CHECK (((normal_first_response_minutes > 0) AND (normal_resolution_minutes > 0) AND (high_first_response_minutes > 0) AND (high_resolution_minutes > 0) AND (critical_first_response_minutes > 0) AND (critical_resolution_minutes > 0))),
    CONSTRAINT sla_policies_working_days_check CHECK ((working_days <@ ARRAY[1, 2, 3, 4, 5, 6, 7]))
);


--
-- Name: subscriptions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.subscriptions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_id uuid,
    plan_id uuid,
    status character varying(20) DEFAULT 'active'::character varying,
    current_period_start timestamp with time zone,
    current_period_end timestamp with time zone,
    stripe_subscription_id character varying(100),
    stripe_customer_id character varying(100),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    cancel_at timestamp with time zone,
    CONSTRAINT subscriptions_status_check CHECK (((status)::text = ANY (ARRAY[('active'::character varying)::text, ('cancelled'::character varying)::text, ('past_due'::character varying)::text, ('trialing'::character varying)::text])))
);


--
-- Name: system_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_logs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    "timestamp" timestamp with time zone DEFAULT now(),
    user_id uuid,
    admin_id uuid,
    company_id uuid,
    action_type character varying(100) NOT NULL,
    resource_type character varying(50),
    resource_id uuid,
    details jsonb,
    ip_address inet,
    user_agent text,
    session_id text,
    status character varying(20),
    error_message text,
    CONSTRAINT system_logs_status_check CHECK (((status)::text = ANY (ARRAY[('success'::character varying)::text, ('error'::character varying)::text, ('warning'::character varying)::text])))
);


--
-- Name: token_usage_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.token_usage_logs (
    id uuid DEFAULT extensions.uuid_generate_v4() NOT NULL,
    company_id uuid,
    agent_id uuid,
    service_type text NOT NULL,
    model_name text NOT NULL,
    input_tokens integer DEFAULT 0,
    output_tokens integer DEFAULT 0,
    total_cost_usd numeric(10,6) DEFAULT 0,
    details jsonb,
    created_at timestamp with time zone DEFAULT now(),
    billed boolean DEFAULT false,
    billed_at timestamp with time zone,
    cache_creation_tokens integer DEFAULT 0,
    cache_read_tokens integer DEFAULT 0,
    cached_tokens integer DEFAULT 0,
    idempotency_key uuid DEFAULT gen_random_uuid()
);


--
-- Name: token_usage_outbox; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.token_usage_outbox (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    idempotency_key uuid NOT NULL,
    company_id uuid,
    payload jsonb NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    max_attempts integer DEFAULT 10 NOT NULL,
    dead_at timestamp with time zone,
    last_error text,
    claimed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: ucp_connections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ucp_connections (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_id uuid NOT NULL,
    company_id uuid NOT NULL,
    store_url text NOT NULL,
    manifest_cached jsonb,
    manifest_version text,
    preferred_transport character varying(10) DEFAULT 'rest'::character varying,
    capabilities_enabled text[] DEFAULT '{}'::text[],
    is_active boolean DEFAULT true,
    last_used_at timestamp with time zone,
    last_error text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    config_updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE ucp_connections; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.ucp_connections IS 'Conexões UCP entre agentes e lojas. Usa discovery-based approach via /.well-known/ucp';


--
-- Name: COLUMN ucp_connections.store_url; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ucp_connections.store_url IS 'URL completa da loja (ex: https://minhaloja.com.br)';


--
-- Name: COLUMN ucp_connections.manifest_cached; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ucp_connections.manifest_cached IS 'Manifest UCP completo em formato JSON, cacheado do discovery';


--
-- Name: COLUMN ucp_connections.preferred_transport; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ucp_connections.preferred_transport IS 'Transport preferido: rest, mcp ou a2a';


--
-- Name: COLUMN ucp_connections.capabilities_enabled; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ucp_connections.capabilities_enabled IS 'Lista de capabilities UCP habilitadas (ex: dev.ucp.shopping.checkout)';


--
-- Name: COLUMN ucp_connections.config_updated_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.ucp_connections.config_updated_at IS 'Timestamp of the last configuration change, used by the Tool Registry cache fingerprint. Operational columns (last_used_at, last_error) do not touch it.';


--
-- Name: ucp_connection_summary; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.ucp_connection_summary AS
 SELECT uc.id,
    uc.agent_id,
    uc.company_id,
    uc.store_url,
    uc.manifest_version,
    uc.preferred_transport,
    array_length(uc.capabilities_enabled, 1) AS capabilities_count,
    uc.is_active,
    uc.last_used_at,
    uc.created_at,
    a.name AS agent_name
   FROM (public.ucp_connections uc
     LEFT JOIN public.agents a ON ((uc.agent_id = a.id)))
  WHERE (uc.is_active = true);


--
-- Name: user_memories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_memories (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    company_id uuid NOT NULL,
    profile jsonb DEFAULT '{}'::jsonb,
    facts text[] DEFAULT '{}'::text[],
    facts_metadata jsonb DEFAULT '[]'::jsonb,
    facts_count integer DEFAULT 0,
    last_extraction_at timestamp with time zone,
    last_consolidation_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    agent_id uuid
);


--
-- Name: users_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users_v2 (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    email character varying(255) NOT NULL,
    password_hash text,
    first_name character varying(100) NOT NULL,
    last_name character varying(100) NOT NULL,
    cpf character varying(14) NOT NULL,
    phone character varying(20) NOT NULL,
    birth_date date NOT NULL,
    company_id uuid,
    status character varying(20) DEFAULT 'pending'::character varying,
    plan_id uuid,
    plan_status character varying(20) DEFAULT 'active'::character varying,
    subscription_amount numeric(10,2),
    billing_cycle character varying(20),
    subscription_started_at timestamp without time zone,
    subscription_renews_at timestamp without time zone,
    subscription_canceled_at timestamp without time zone,
    stripe_customer_id character varying(255),
    stripe_subscription_id character varying(255),
    credits_used_this_month integer DEFAULT 0,
    credits_limit integer,
    storage_used_mb numeric(10,2) DEFAULT 0,
    storage_limit_mb integer,
    usage_reset_date date,
    last_login_at timestamp without time zone,
    last_login_ip inet,
    failed_login_attempts integer DEFAULT 0,
    account_locked_until timestamp without time zone,
    terms_accepted_at timestamp without time zone NOT NULL,
    privacy_policy_accepted_at timestamp without time zone NOT NULL,
    marketing_consent boolean DEFAULT false,
    data_deletion_requested_at timestamp without time zone,
    google_id character varying(255),
    github_id character varying(255),
    oauth_provider character varying(20) DEFAULT 'email'::character varying,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now(),
    deleted_at timestamp without time zone,
    role character varying(20) DEFAULT 'member'::character varying,
    is_owner boolean DEFAULT false,
    avatar_url text,
    reset_token text,
    reset_token_expires_at timestamp with time zone,
    password_migrated_at timestamp with time zone,
    reset_attempts integer DEFAULT 0,
    accepted_terms_version uuid,
    CONSTRAINT users_v2_billing_cycle_check CHECK (((billing_cycle)::text = ANY (ARRAY[('monthly'::character varying)::text, ('yearly'::character varying)::text]))),
    CONSTRAINT users_v2_oauth_provider_check CHECK (((oauth_provider)::text = ANY (ARRAY[('email'::character varying)::text, ('google'::character varying)::text, ('github'::character varying)::text]))),
    CONSTRAINT users_v2_plan_status_check CHECK (((plan_status)::text = ANY (ARRAY[('active'::character varying)::text, ('past_due'::character varying)::text, ('canceled'::character varying)::text, ('suspended'::character varying)::text]))),
    CONSTRAINT users_v2_role_check CHECK (((role)::text = ANY (ARRAY[('admin_company'::character varying)::text, ('member'::character varying)::text]))),
    CONSTRAINT users_v2_status_check CHECK (((status)::text = ANY (ARRAY[('pending'::character varying)::text, ('active'::character varying)::text, ('suspended'::character varying)::text, ('lead'::character varying)::text])))
);


--
-- Name: widget_rate_limits; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.widget_rate_limits (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    identifier character varying(255) NOT NULL,
    identifier_type character varying(20) DEFAULT 'session'::character varying,
    request_count integer DEFAULT 1,
    window_start timestamp with time zone DEFAULT now(),
    agent_id uuid,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: app_runtime_secrets app_runtime_secrets_pkey; Type: CONSTRAINT; Schema: private; Owner: -
--

ALTER TABLE ONLY private.app_runtime_secrets
    ADD CONSTRAINT app_runtime_secrets_pkey PRIMARY KEY (name);


--
-- Name: admin_users admin_users_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_users
    ADD CONSTRAINT admin_users_email_key UNIQUE (email);


--
-- Name: admin_users admin_users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_users
    ADD CONSTRAINT admin_users_pkey PRIMARY KEY (id);


--
-- Name: agent_attendance_settings agent_attendance_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_attendance_settings
    ADD CONSTRAINT agent_attendance_settings_pkey PRIMARY KEY (agent_id);


--
-- Name: agent_delegations agent_delegations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_delegations
    ADD CONSTRAINT agent_delegations_pkey PRIMARY KEY (id);


--
-- Name: agent_http_tools agent_http_tools_agent_id_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_http_tools
    ADD CONSTRAINT agent_http_tools_agent_id_name_key UNIQUE (agent_id, name);


--
-- Name: agent_http_tools agent_http_tools_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_http_tools
    ADD CONSTRAINT agent_http_tools_pkey PRIMARY KEY (id);


--
-- Name: agent_mcp_connections agent_mcp_connections_agent_id_mcp_server_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_mcp_connections
    ADD CONSTRAINT agent_mcp_connections_agent_id_mcp_server_id_key UNIQUE (agent_id, mcp_server_id);


--
-- Name: agent_mcp_connections agent_mcp_connections_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_mcp_connections
    ADD CONSTRAINT agent_mcp_connections_pkey PRIMARY KEY (id);


--
-- Name: agent_mcp_tools agent_mcp_tools_agent_id_mcp_server_id_tool_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_mcp_tools
    ADD CONSTRAINT agent_mcp_tools_agent_id_mcp_server_id_tool_name_key UNIQUE (agent_id, mcp_server_id, tool_name);


--
-- Name: agent_mcp_tools agent_mcp_tools_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_mcp_tools
    ADD CONSTRAINT agent_mcp_tools_pkey PRIMARY KEY (id);


--
-- Name: agents agents_company_id_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_company_id_slug_key UNIQUE (company_id, slug);


--
-- Name: agents agents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_pkey PRIMARY KEY (id);


--
-- Name: attendance_sessions attendance_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_sessions
    ADD CONSTRAINT attendance_sessions_pkey PRIMARY KEY (id);


--
-- Name: attendance_sla attendance_sla_attendance_session_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_sla
    ADD CONSTRAINT attendance_sla_attendance_session_id_key UNIQUE (attendance_session_id);


--
-- Name: attendance_sla attendance_sla_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_sla
    ADD CONSTRAINT attendance_sla_pkey PRIMARY KEY (id);


--
-- Name: checkpoint_blobs checkpoint_blobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checkpoint_blobs
    ADD CONSTRAINT checkpoint_blobs_pkey PRIMARY KEY (thread_id, checkpoint_ns, channel, version);


--
-- Name: checkpoint_migrations checkpoint_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checkpoint_migrations
    ADD CONSTRAINT checkpoint_migrations_pkey PRIMARY KEY (v);


--
-- Name: checkpoint_writes checkpoint_writes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checkpoint_writes
    ADD CONSTRAINT checkpoint_writes_pkey PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx);


--
-- Name: checkpoints checkpoints_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checkpoints
    ADD CONSTRAINT checkpoints_pkey PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id);


--
-- Name: companies companies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.companies
    ADD CONSTRAINT companies_pkey PRIMARY KEY (id);


--
-- Name: company_attendance_settings company_attendance_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_attendance_settings
    ADD CONSTRAINT company_attendance_settings_pkey PRIMARY KEY (company_id);


--
-- Name: conversation_events conversation_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_events
    ADD CONSTRAINT conversation_events_pkey PRIMARY KEY (id);


--
-- Name: conversation_inactivity_timers conversation_inactivity_timers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_inactivity_timers
    ADD CONSTRAINT conversation_inactivity_timers_pkey PRIMARY KEY (id);


--
-- Name: conversation_logs conversation_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_logs
    ADD CONSTRAINT conversation_logs_pkey PRIMARY KEY (id);


--
-- Name: conversations conversations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT conversations_pkey PRIMARY KEY (id);


--
-- Name: credit_transactions credit_transactions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_transactions
    ADD CONSTRAINT credit_transactions_pkey PRIMARY KEY (id);


--
-- Name: documents documents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_pkey PRIMARY KEY (id);


--
-- Name: handoff_notification_recipients handoff_notification_recipients_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.handoff_notification_recipients
    ADD CONSTRAINT handoff_notification_recipients_pkey PRIMARY KEY (id);


--
-- Name: integrations integrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.integrations
    ADD CONSTRAINT integrations_pkey PRIMARY KEY (id);


--
-- Name: integrations integrations_provider_identifier_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.integrations
    ADD CONSTRAINT integrations_provider_identifier_key UNIQUE (provider, identifier);


--
-- Name: internal_whatsapp_blocklist internal_whatsapp_blocklist_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.internal_whatsapp_blocklist
    ADD CONSTRAINT internal_whatsapp_blocklist_pkey PRIMARY KEY (id);


--
-- Name: invites invites_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invites
    ADD CONSTRAINT invites_pkey PRIMARY KEY (id);


--
-- Name: invites invites_token_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invites
    ADD CONSTRAINT invites_token_key UNIQUE (token);


--
-- Name: leads leads_company_id_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leads
    ADD CONSTRAINT leads_company_id_email_key UNIQUE (company_id, email);


--
-- Name: leads leads_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leads
    ADD CONSTRAINT leads_pkey PRIMARY KEY (id);


--
-- Name: legal_documents legal_documents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.legal_documents
    ADD CONSTRAINT legal_documents_pkey PRIMARY KEY (id);


--
-- Name: llm_pricing llm_pricing_model_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_pricing
    ADD CONSTRAINT llm_pricing_model_name_key UNIQUE (model_name);


--
-- Name: llm_pricing llm_pricing_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_pricing
    ADD CONSTRAINT llm_pricing_pkey PRIMARY KEY (id);


--
-- Name: mcp_oauth_clients mcp_oauth_clients_mcp_server_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_oauth_clients
    ADD CONSTRAINT mcp_oauth_clients_mcp_server_id_key UNIQUE (mcp_server_id);


--
-- Name: mcp_oauth_clients mcp_oauth_clients_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_oauth_clients
    ADD CONSTRAINT mcp_oauth_clients_pkey PRIMARY KEY (id);


--
-- Name: mcp_servers mcp_servers_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_servers
    ADD CONSTRAINT mcp_servers_name_key UNIQUE (name);


--
-- Name: mcp_servers mcp_servers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_servers
    ADD CONSTRAINT mcp_servers_pkey PRIMARY KEY (id);


--
-- Name: memory_processing_locks memory_processing_locks_session_id_company_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_processing_locks
    ADD CONSTRAINT memory_processing_locks_session_id_company_id_key UNIQUE (session_id, company_id);


--
-- Name: memory_settings memory_settings_agent_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_settings
    ADD CONSTRAINT memory_settings_agent_id_key UNIQUE (agent_id);


--
-- Name: memory_settings memory_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_settings
    ADD CONSTRAINT memory_settings_pkey PRIMARY KEY (id);


--
-- Name: messages messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_pkey PRIMARY KEY (id);


--
-- Name: notification_deliveries notification_deliveries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_deliveries
    ADD CONSTRAINT notification_deliveries_pkey PRIMARY KEY (id);


--
-- Name: password_reset_tokens password_reset_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_pkey PRIMARY KEY (id);


--
-- Name: password_reset_tokens password_reset_tokens_token_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_token_key UNIQUE (token);


--
-- Name: payment_history payment_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_history
    ADD CONSTRAINT payment_history_pkey PRIMARY KEY (id);


--
-- Name: plans plans_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plans
    ADD CONSTRAINT plans_pkey PRIMARY KEY (id);


--
-- Name: plans plans_slug_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plans
    ADD CONSTRAINT plans_slug_key UNIQUE (slug);


--
-- Name: platform_provider_alerts platform_provider_alerts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_provider_alerts
    ADD CONSTRAINT platform_provider_alerts_pkey PRIMARY KEY (provider);


--
-- Name: platform_settings platform_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_settings
    ADD CONSTRAINT platform_settings_pkey PRIMARY KEY (key);


--
-- Name: sanitization_jobs sanitization_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sanitization_jobs
    ADD CONSTRAINT sanitization_jobs_pkey PRIMARY KEY (id);


--
-- Name: session_summaries session_summaries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_summaries
    ADD CONSTRAINT session_summaries_pkey PRIMARY KEY (id);


--
-- Name: sla_events sla_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sla_events
    ADD CONSTRAINT sla_events_pkey PRIMARY KEY (id);


--
-- Name: sla_policies sla_policies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sla_policies
    ADD CONSTRAINT sla_policies_pkey PRIMARY KEY (id);


--
-- Name: subscriptions subscriptions_company_id_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_company_id_unique UNIQUE (company_id);


--
-- Name: subscriptions subscriptions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_pkey PRIMARY KEY (id);


--
-- Name: system_logs system_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_logs
    ADD CONSTRAINT system_logs_pkey PRIMARY KEY (id);


--
-- Name: company_credits tenant_credits_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_credits
    ADD CONSTRAINT tenant_credits_pkey PRIMARY KEY (id);


--
-- Name: company_credits tenant_credits_tenant_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_credits
    ADD CONSTRAINT tenant_credits_tenant_id_key UNIQUE (company_id);


--
-- Name: token_usage_logs token_usage_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_usage_logs
    ADD CONSTRAINT token_usage_logs_pkey PRIMARY KEY (id);


--
-- Name: token_usage_outbox token_usage_outbox_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_usage_outbox
    ADD CONSTRAINT token_usage_outbox_pkey PRIMARY KEY (id);


--
-- Name: ucp_connections ucp_connections_agent_id_store_url_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ucp_connections
    ADD CONSTRAINT ucp_connections_agent_id_store_url_key UNIQUE (agent_id, store_url);


--
-- Name: ucp_connections ucp_connections_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ucp_connections
    ADD CONSTRAINT ucp_connections_pkey PRIMARY KEY (id);


--
-- Name: agent_delegations unique_delegation; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_delegations
    ADD CONSTRAINT unique_delegation UNIQUE (orchestrator_id, subagent_id);


--
-- Name: widget_rate_limits uq_rate_limit_identifier_agent; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.widget_rate_limits
    ADD CONSTRAINT uq_rate_limit_identifier_agent UNIQUE (identifier, agent_id, identifier_type);


--
-- Name: user_memories user_memories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_memories
    ADD CONSTRAINT user_memories_pkey PRIMARY KEY (id);


--
-- Name: users_v2 users_v2_cpf_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users_v2
    ADD CONSTRAINT users_v2_cpf_key UNIQUE (cpf);


--
-- Name: users_v2 users_v2_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users_v2
    ADD CONSTRAINT users_v2_email_key UNIQUE (email);


--
-- Name: users_v2 users_v2_github_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users_v2
    ADD CONSTRAINT users_v2_github_id_key UNIQUE (github_id);


--
-- Name: users_v2 users_v2_google_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users_v2
    ADD CONSTRAINT users_v2_google_id_key UNIQUE (google_id);


--
-- Name: users_v2 users_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users_v2
    ADD CONSTRAINT users_v2_pkey PRIMARY KEY (id);


--
-- Name: widget_rate_limits widget_rate_limits_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.widget_rate_limits
    ADD CONSTRAINT widget_rate_limits_pkey PRIMARY KEY (id);


--
-- Name: checkpoint_blobs_thread_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX checkpoint_blobs_thread_id_idx ON public.checkpoint_blobs USING btree (thread_id);


--
-- Name: checkpoint_writes_thread_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX checkpoint_writes_thread_id_idx ON public.checkpoint_writes USING btree (thread_id);


--
-- Name: checkpoints_thread_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX checkpoints_thread_id_idx ON public.checkpoints USING btree (thread_id);


--
-- Name: idx_admin_users_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_admin_users_company_id ON public.admin_users USING btree (company_id);


--
-- Name: idx_admin_users_email; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_admin_users_email ON public.admin_users USING btree (email);


--
-- Name: idx_admin_users_reset_token; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_admin_users_reset_token ON public.admin_users USING btree (reset_token) WHERE (reset_token IS NOT NULL);


--
-- Name: idx_agent_attendance_settings_company; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_attendance_settings_company ON public.agent_attendance_settings USING btree (company_id);


--
-- Name: idx_agent_delegations_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_delegations_company_id ON public.agent_delegations USING btree (company_id);


--
-- Name: idx_agent_http_tools_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_http_tools_agent_id ON public.agent_http_tools USING btree (agent_id);


--
-- Name: idx_agent_http_tools_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_http_tools_company_id ON public.agent_http_tools USING btree (company_id);


--
-- Name: idx_agent_mcp_connections_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_mcp_connections_agent ON public.agent_mcp_connections USING btree (agent_id);


--
-- Name: idx_agent_mcp_connections_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_mcp_connections_company_id ON public.agent_mcp_connections USING btree (company_id);


--
-- Name: idx_agent_mcp_tools_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_mcp_tools_agent ON public.agent_mcp_tools USING btree (agent_id);


--
-- Name: idx_agent_mcp_tools_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_mcp_tools_company_id ON public.agent_mcp_tools USING btree (company_id);


--
-- Name: idx_agent_mcp_tools_variable; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_mcp_tools_variable ON public.agent_mcp_tools USING btree (variable_name);


--
-- Name: idx_agents_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agents_company_id ON public.agents USING btree (company_id);


--
-- Name: idx_agents_is_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agents_is_active ON public.agents USING btree (is_active);


--
-- Name: idx_attendance_sessions_company_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_attendance_sessions_company_status ON public.attendance_sessions USING btree (company_id, status, started_at DESC);


--
-- Name: idx_attendance_sessions_company_taken; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_attendance_sessions_company_taken ON public.attendance_sessions USING btree (company_id, human_taken_at);


--
-- Name: idx_attendance_sessions_conversation_started; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_attendance_sessions_conversation_started ON public.attendance_sessions USING btree (conversation_id, started_at DESC);


--
-- Name: idx_attendance_sla_company_first_response_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_attendance_sla_company_first_response_status ON public.attendance_sla USING btree (company_id, first_response_status);


--
-- Name: idx_attendance_sla_company_health_deadline; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_attendance_sla_company_health_deadline ON public.attendance_sla USING btree (company_id, health_status, resolution_deadline);


--
-- Name: idx_attendance_sla_company_resolution_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_attendance_sla_company_resolution_status ON public.attendance_sla USING btree (company_id, resolution_status);


--
-- Name: idx_attendance_sla_first_response_deadline; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_attendance_sla_first_response_deadline ON public.attendance_sla USING btree (company_id, first_response_deadline) WHERE (first_response_status = 'pending'::text);


--
-- Name: idx_attendance_sla_pending_resolution; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_attendance_sla_pending_resolution ON public.attendance_sla USING btree (resolution_deadline, health_status) WHERE (resolution_status = 'pending'::text);


--
-- Name: idx_attendance_sla_worker_first_response; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_attendance_sla_worker_first_response ON public.attendance_sla USING btree (first_response_deadline) WHERE (first_response_status = 'pending'::text);


--
-- Name: idx_checkpoints_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_checkpoints_company_id ON public.checkpoints USING btree (company_id);


--
-- Name: idx_companies_agent_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_companies_agent_enabled ON public.companies USING btree (agent_enabled) WHERE (agent_enabled = true);


--
-- Name: idx_companies_allow_vision; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_companies_allow_vision ON public.companies USING btree (allow_vision) WHERE (allow_vision = true);


--
-- Name: idx_companies_llm_provider; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_companies_llm_provider ON public.companies USING btree (llm_provider) WHERE (llm_provider IS NOT NULL);


--
-- Name: idx_companies_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_companies_status ON public.companies USING btree (status);


--
-- Name: idx_companies_use_langchain; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_companies_use_langchain ON public.companies USING btree (use_langchain);


--
-- Name: idx_companies_webhook; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_companies_webhook ON public.companies USING btree (webhook_url);


--
-- Name: idx_company_credits_company; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_company_credits_company ON public.company_credits USING btree (company_id);


--
-- Name: idx_conversation_events_company_type_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversation_events_company_type_created ON public.conversation_events USING btree (company_id, event_type, created_at DESC);


--
-- Name: idx_conversation_events_conversation_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversation_events_conversation_created ON public.conversation_events USING btree (conversation_id, created_at DESC);


--
-- Name: idx_conversation_logs_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversation_logs_agent_id ON public.conversation_logs USING btree (agent_id);


--
-- Name: idx_conversation_logs_company; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversation_logs_company ON public.conversation_logs USING btree (company_id);


--
-- Name: idx_conversation_logs_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversation_logs_created ON public.conversation_logs USING btree (created_at DESC);


--
-- Name: idx_conversation_logs_model; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversation_logs_model ON public.conversation_logs USING btree (llm_provider, llm_model);


--
-- Name: idx_conversation_logs_search_strategy; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversation_logs_search_strategy ON public.conversation_logs USING btree (search_strategy) WHERE (search_strategy IS NOT NULL);


--
-- Name: idx_conversation_logs_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversation_logs_session ON public.conversation_logs USING btree (session_id);


--
-- Name: idx_conversation_logs_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversation_logs_timestamp ON public.conversation_logs USING btree ("timestamp" DESC);


--
-- Name: idx_conversation_logs_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversation_logs_user ON public.conversation_logs USING btree (user_id);


--
-- Name: idx_conversations_admin_list; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversations_admin_list ON public.conversations USING btree (company_id, status);


--
-- Name: idx_conversations_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversations_agent_id ON public.conversations USING btree (agent_id);


--
-- Name: idx_conversations_company_admin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversations_company_admin ON public.conversations USING btree (company_id, last_message_at DESC);


--
-- Name: idx_conversations_company_agent_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversations_company_agent_status ON public.conversations USING btree (company_id, agent_id, status, last_message_at DESC);


--
-- Name: idx_conversations_company_assigned_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversations_company_assigned_status ON public.conversations USING btree (company_id, assigned_user_id, status);


--
-- Name: idx_conversations_company_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversations_company_created ON public.conversations USING btree (company_id, created_at);


--
-- Name: idx_conversations_company_status_last_message; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversations_company_status_last_message ON public.conversations USING btree (company_id, status, last_message_at DESC);


--
-- Name: idx_conversations_company_user_channel; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversations_company_user_channel ON public.conversations USING btree (company_id, user_id, channel);


--
-- Name: idx_conversations_current_attendance_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversations_current_attendance_session ON public.conversations USING btree (current_attendance_session_id);


--
-- Name: idx_conversations_session_company; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversations_session_company ON public.conversations USING btree (session_id, company_id);


--
-- Name: idx_conversations_session_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversations_session_id ON public.conversations USING btree (session_id);


--
-- Name: idx_conversations_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_conversations_user_id ON public.conversations USING btree (user_id);


--
-- Name: idx_credit_transactions_company; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_credit_transactions_company ON public.credit_transactions USING btree (company_id);


--
-- Name: idx_credit_transactions_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_credit_transactions_created ON public.credit_transactions USING btree (created_at DESC);


--
-- Name: idx_credit_transactions_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_credit_transactions_tenant ON public.credit_transactions USING btree (company_id);


--
-- Name: idx_credit_transactions_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_credit_transactions_type ON public.credit_transactions USING btree (type);


--
-- Name: idx_delegations_orchestrator; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_delegations_orchestrator ON public.agent_delegations USING btree (orchestrator_id) WHERE (is_active = true);


--
-- Name: idx_documents_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_documents_agent_id ON public.documents USING btree (agent_id);


--
-- Name: idx_documents_agent_ingestion; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_documents_agent_ingestion ON public.documents USING btree (agent_id, ingestion_mode);


--
-- Name: idx_documents_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_documents_company_id ON public.documents USING btree (company_id);


--
-- Name: idx_documents_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_documents_created_at ON public.documents USING btree (created_at DESC);


--
-- Name: idx_documents_ingestion_mode; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_documents_ingestion_mode ON public.documents USING btree (ingestion_mode);


--
-- Name: idx_documents_ingestion_strategy; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_documents_ingestion_strategy ON public.documents USING btree (ingestion_strategy);


--
-- Name: idx_documents_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_documents_status ON public.documents USING btree (status);


--
-- Name: idx_documents_strategy; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_documents_strategy ON public.documents USING btree (ingestion_strategy);


--
-- Name: idx_handoff_recipients_company_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_handoff_recipients_company_agent ON public.handoff_notification_recipients USING btree (company_id, agent_id, channel) WHERE (enabled = true);


--
-- Name: idx_inactivity_timers_conversation_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_inactivity_timers_conversation_status ON public.conversation_inactivity_timers USING btree (conversation_id, status);


--
-- Name: idx_inactivity_timers_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_inactivity_timers_due ON public.conversation_inactivity_timers USING btree (next_action_at) WHERE (status = 'scheduled'::text);


--
-- Name: idx_integrations_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_integrations_agent_id ON public.integrations USING btree (agent_id);


--
-- Name: idx_integrations_identifier; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_integrations_identifier ON public.integrations USING btree (identifier);


--
-- Name: idx_invites_role; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_invites_role ON public.invites USING btree (role);


--
-- Name: idx_invites_token; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_invites_token ON public.invites USING btree (token);


--
-- Name: idx_leads_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_leads_lookup ON public.leads USING btree (company_id, email);


--
-- Name: idx_legal_docs_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_legal_docs_active ON public.legal_documents USING btree (type, is_active) WHERE (is_active = true);


--
-- Name: idx_llm_pricing_model; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_llm_pricing_model ON public.llm_pricing USING btree (model_name);


--
-- Name: idx_memory_locks_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_locks_agent_id ON public.memory_processing_locks USING btree (agent_id);


--
-- Name: idx_memory_locks_company; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_locks_company ON public.memory_processing_locks USING btree (company_id);


--
-- Name: idx_memory_locks_processing; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_locks_processing ON public.memory_processing_locks USING btree (is_processing) WHERE (is_processing = true);


--
-- Name: idx_memory_locks_scheduled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_locks_scheduled ON public.memory_processing_locks USING btree (scheduled_for) WHERE (scheduled_for IS NOT NULL);


--
-- Name: idx_memory_locks_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_locks_session ON public.memory_processing_locks USING btree (session_id);


--
-- Name: idx_memory_locks_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_memory_locks_unique ON public.memory_processing_locks USING btree (session_id, company_id);


--
-- Name: idx_memory_settings_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_settings_agent ON public.memory_settings USING btree (agent_id);


--
-- Name: idx_memory_settings_company; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_settings_company ON public.memory_settings USING btree (company_id);


--
-- Name: idx_messages_ai_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_ai_created ON public.messages USING btree (created_at) WHERE ((role = 'assistant'::text) AND (sender_user_id IS NULL));


--
-- Name: idx_messages_by_conversation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_by_conversation ON public.messages USING btree (conversation_id, created_at);


--
-- Name: idx_messages_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_company_id ON public.messages USING btree (company_id);


--
-- Name: idx_messages_conversation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_conversation_id ON public.messages USING btree (conversation_id);


--
-- Name: idx_messages_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_created_at ON public.messages USING btree (created_at);


--
-- Name: idx_messages_sender_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_sender_user_id ON public.messages USING btree (sender_user_id);


--
-- Name: idx_password_reset_tokens_token; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_password_reset_tokens_token ON public.password_reset_tokens USING btree (token);


--
-- Name: idx_password_reset_tokens_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_password_reset_tokens_user_id ON public.password_reset_tokens USING btree (user_id);


--
-- Name: idx_payment_history_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_payment_history_status ON public.payment_history USING btree (status);


--
-- Name: idx_payment_history_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_payment_history_user_id ON public.payment_history USING btree (user_id);


--
-- Name: idx_plans_is_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_plans_is_active ON public.plans USING btree (is_active);


--
-- Name: idx_plans_slug; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_plans_slug ON public.plans USING btree (slug);


--
-- Name: idx_sanitization_jobs_company; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sanitization_jobs_company ON public.sanitization_jobs USING btree (company_id);


--
-- Name: idx_sanitization_jobs_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sanitization_jobs_expires ON public.sanitization_jobs USING btree (expires_at);


--
-- Name: idx_sanitization_jobs_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sanitization_jobs_status ON public.sanitization_jobs USING btree (status);


--
-- Name: idx_session_summaries_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_session_summaries_agent_id ON public.session_summaries USING btree (agent_id);


--
-- Name: idx_session_summaries_company; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_session_summaries_company ON public.session_summaries USING btree (company_id);


--
-- Name: idx_session_summaries_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_session_summaries_created ON public.session_summaries USING btree (created_at DESC);


--
-- Name: idx_session_summaries_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_session_summaries_session ON public.session_summaries USING btree (session_id);


--
-- Name: idx_session_summaries_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_session_summaries_user ON public.session_summaries USING btree (user_id);


--
-- Name: idx_sla_events_company_type_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sla_events_company_type_created ON public.sla_events USING btree (company_id, event_type, created_at);


--
-- Name: idx_subscriptions_company; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_subscriptions_company ON public.subscriptions USING btree (company_id);


--
-- Name: idx_subscriptions_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_subscriptions_company_id ON public.subscriptions USING btree (company_id);


--
-- Name: idx_subscriptions_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_subscriptions_status ON public.subscriptions USING btree (status);


--
-- Name: idx_subscriptions_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_subscriptions_tenant ON public.subscriptions USING btree (company_id);


--
-- Name: idx_system_logs_action_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_system_logs_action_type ON public.system_logs USING btree (action_type);


--
-- Name: idx_system_logs_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_system_logs_status ON public.system_logs USING btree (status);


--
-- Name: idx_system_logs_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_system_logs_timestamp ON public.system_logs USING btree ("timestamp");


--
-- Name: idx_system_logs_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_system_logs_user_id ON public.system_logs USING btree (user_id);


--
-- Name: idx_tenant_credits_tenant; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tenant_credits_tenant ON public.company_credits USING btree (company_id);


--
-- Name: idx_token_usage_company; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_usage_company ON public.token_usage_logs USING btree (company_id);


--
-- Name: idx_token_usage_company_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_usage_company_date ON public.token_usage_logs USING btree (company_id, created_at DESC);


--
-- Name: idx_token_usage_company_unbilled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_usage_company_unbilled ON public.token_usage_logs USING btree (company_id, created_at) WHERE (billed = false);


--
-- Name: idx_token_usage_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_usage_created ON public.token_usage_logs USING btree (created_at);


--
-- Name: idx_token_usage_model; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_usage_model ON public.token_usage_logs USING btree (model_name);


--
-- Name: idx_token_usage_service; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_usage_service ON public.token_usage_logs USING btree (service_type);


--
-- Name: idx_token_usage_unbilled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_usage_unbilled ON public.token_usage_logs USING btree (billed, created_at) WHERE (billed = false);


--
-- Name: idx_ucp_connections_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ucp_connections_active ON public.ucp_connections USING btree (is_active) WHERE (is_active = true);


--
-- Name: idx_ucp_connections_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ucp_connections_agent ON public.ucp_connections USING btree (agent_id);


--
-- Name: idx_ucp_connections_capabilities; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ucp_connections_capabilities ON public.ucp_connections USING gin (capabilities_enabled);


--
-- Name: idx_ucp_connections_company; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ucp_connections_company ON public.ucp_connections USING btree (company_id);


--
-- Name: idx_ucp_connections_store; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ucp_connections_store ON public.ucp_connections USING btree (store_url);


--
-- Name: idx_user_memories_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_user_memories_agent_id ON public.user_memories USING btree (agent_id);


--
-- Name: idx_user_memories_company; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_user_memories_company ON public.user_memories USING btree (company_id);


--
-- Name: idx_user_memories_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_user_memories_user ON public.user_memories USING btree (user_id);


--
-- Name: idx_user_memories_user_company_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_user_memories_user_company_agent ON public.user_memories USING btree (user_id, company_id, COALESCE(agent_id, '00000000-0000-0000-0000-000000000000'::uuid));


--
-- Name: idx_users_v2_company_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_users_v2_company_id ON public.users_v2 USING btree (company_id);


--
-- Name: idx_users_v2_cpf; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_users_v2_cpf ON public.users_v2 USING btree (cpf);


--
-- Name: idx_users_v2_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_users_v2_deleted_at ON public.users_v2 USING btree (deleted_at);


--
-- Name: idx_users_v2_email; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_users_v2_email ON public.users_v2 USING btree (email);


--
-- Name: idx_users_v2_github_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_users_v2_github_id ON public.users_v2 USING btree (github_id);


--
-- Name: idx_users_v2_google_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_users_v2_google_id ON public.users_v2 USING btree (google_id);


--
-- Name: idx_users_v2_not_migrated; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_users_v2_not_migrated ON public.users_v2 USING btree (id) WHERE (password_migrated_at IS NULL);


--
-- Name: idx_users_v2_plan_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_users_v2_plan_id ON public.users_v2 USING btree (plan_id);


--
-- Name: idx_users_v2_reset_token; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_users_v2_reset_token ON public.users_v2 USING btree (reset_token) WHERE (reset_token IS NOT NULL);


--
-- Name: ix_token_usage_outbox_claim; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_token_usage_outbox_claim ON public.token_usage_outbox USING btree (dead_at, claimed_at, created_at);


--
-- Name: uniq_integrations_webhook_token_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uniq_integrations_webhook_token_hash ON public.integrations USING btree (webhook_token_hash) WHERE (webhook_token_hash IS NOT NULL);


--
-- Name: INDEX uniq_integrations_webhook_token_hash; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON INDEX public.uniq_integrations_webhook_token_hash IS 'Lookup O(1) + unicidade do token de webhook por-integração. Parcial em webhook_token_hash IS NOT NULL para não colidir as linhas legadas NULL durante o rollout (pré-backfill). Provider-agnóstico de propósito (D7): token opaco de 256 bits já é globalmente único, sem `provider IN` (evita 4ª ocorrência do literal canônico {z-api,uazapi,evolution}).';


--
-- Name: uniq_whatsapp_active_integration_per_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uniq_whatsapp_active_integration_per_agent ON public.integrations USING btree (agent_id) WHERE (((provider)::text = ANY ((ARRAY['z-api'::character varying, 'uazapi'::character varying, 'evolution'::character varying])::text[])) AND (agent_id IS NOT NULL) AND (is_active = true));


--
-- Name: INDEX uniq_whatsapp_active_integration_per_agent; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON INDEX public.uniq_whatsapp_active_integration_per_agent IS 'Exclusividade WhatsApp: no máximo UMA integração WhatsApp ATIVA por agente, sobre o conjunto canônico estreitado {z-api, uazapi, evolution}. Parcial em is_active=true — linhas inativas (histórico) e globais (agent_id IS NULL) não colidem. Violação => 23505, mapeado para HTTP 409 pelo route.ts. Predicado sincronizado com integration_service.WHATSAPP_PROVIDERS e route.ts (invariante de sincronia tripla).';


--
-- Name: uq_attendance_sessions_one_open_per_conversation; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_attendance_sessions_one_open_per_conversation ON public.attendance_sessions USING btree (conversation_id) WHERE (status = ANY (ARRAY['open'::text, 'human_requested'::text, 'human_active'::text, 'pending_customer'::text]));


--
-- Name: uq_conversation_events_idempotency; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_conversation_events_idempotency ON public.conversation_events USING btree (idempotency_key) WHERE (idempotency_key IS NOT NULL);


--
-- Name: uq_conversations_company_agent_session; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_conversations_company_agent_session ON public.conversations USING btree (company_id, COALESCE(agent_id, '00000000-0000-0000-0000-000000000000'::uuid), session_id);


--
-- Name: uq_credit_transactions_idempotency_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_credit_transactions_idempotency_key ON public.credit_transactions USING btree (idempotency_key) WHERE (idempotency_key IS NOT NULL);


--
-- Name: uq_credit_transactions_stripe_payment_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_credit_transactions_stripe_payment_id ON public.credit_transactions USING btree (stripe_payment_id) WHERE (stripe_payment_id IS NOT NULL);


--
-- Name: INDEX uq_credit_transactions_stripe_payment_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON INDEX public.uq_credit_transactions_stripe_payment_id IS 'Idempotência Stripe (F14): impede crédito duplicado sob entrega at-least-once. Parcial — débitos de consumo (stripe_payment_id NULL) não colidem.';


--
-- Name: uq_handoff_recipient_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_handoff_recipient_active ON public.handoff_notification_recipients USING btree (company_id, COALESCE(agent_id, '00000000-0000-0000-0000-000000000000'::uuid), channel, recipient_normalized) WHERE (enabled = true);


--
-- Name: uq_inactivity_timers_one_scheduled; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_inactivity_timers_one_scheduled ON public.conversation_inactivity_timers USING btree (conversation_id, timer_type) WHERE (status = ANY (ARRAY['scheduled'::text, 'processing'::text]));


--
-- Name: uq_internal_whatsapp_blocklist_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_internal_whatsapp_blocklist_scope ON public.internal_whatsapp_blocklist USING btree (company_id, phone_normalized, COALESCE(agent_id, '00000000-0000-0000-0000-000000000000'::uuid), COALESCE(integration_id, '00000000-0000-0000-0000-000000000000'::uuid)) WHERE (active = true);


--
-- Name: uq_notification_delivery_idempotency; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_notification_delivery_idempotency ON public.notification_deliveries USING btree (idempotency_key);


--
-- Name: uq_sla_events_once_per_session_type; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_sla_events_once_per_session_type ON public.sla_events USING btree (attendance_session_id, event_type) WHERE (event_type = ANY (ARRAY['first_response_met'::text, 'first_response_missed'::text, 'at_risk_50pct'::text, 'critical_75pct'::text, 'resolution_breached'::text, 'resolution_met'::text, 'resolution_missed'::text]));


--
-- Name: uq_sla_policies_one_active_per_company; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_sla_policies_one_active_per_company ON public.sla_policies USING btree (company_id) WHERE (is_active = true);


--
-- Name: uq_subscriptions_one_active_per_company; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_subscriptions_one_active_per_company ON public.subscriptions USING btree (company_id) WHERE ((status)::text = 'active'::text);


--
-- Name: ux_token_usage_logs_idempotency_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_token_usage_logs_idempotency_key ON public.token_usage_logs USING btree (idempotency_key);


--
-- Name: agent_mcp_connections agent_mcp_connections_touch_config; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER agent_mcp_connections_touch_config BEFORE UPDATE ON public.agent_mcp_connections FOR EACH ROW EXECUTE FUNCTION public.agent_mcp_connections_touch_config_updated_at();


--
-- Name: admin_users trg_security_audit_admin_users_role; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_security_audit_admin_users_role AFTER UPDATE OF role ON public.admin_users FOR EACH ROW WHEN ((old.role IS DISTINCT FROM new.role)) EXECUTE FUNCTION public.security_audit_admin_users_role();


--
-- Name: agent_http_tools trg_security_audit_agent_http_tools_delete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_security_audit_agent_http_tools_delete AFTER DELETE ON public.agent_http_tools FOR EACH ROW EXECUTE FUNCTION public.security_audit_resource_delete();


--
-- Name: agent_http_tools trg_security_audit_agent_http_tools_url_insert; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_security_audit_agent_http_tools_url_insert AFTER INSERT ON public.agent_http_tools FOR EACH ROW WHEN (((new.url IS NOT NULL) AND (btrim(new.url) <> ''::text))) EXECUTE FUNCTION public.security_audit_agent_http_tools_url();


--
-- Name: agent_http_tools trg_security_audit_agent_http_tools_url_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_security_audit_agent_http_tools_url_update AFTER UPDATE OF url ON public.agent_http_tools FOR EACH ROW WHEN ((old.url IS DISTINCT FROM new.url)) EXECUTE FUNCTION public.security_audit_agent_http_tools_url();


--
-- Name: agent_mcp_connections trg_security_audit_agent_mcp_connections_delete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_security_audit_agent_mcp_connections_delete AFTER DELETE ON public.agent_mcp_connections FOR EACH ROW EXECUTE FUNCTION public.security_audit_resource_delete();


--
-- Name: agents trg_security_audit_agents_delete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_security_audit_agents_delete AFTER DELETE ON public.agents FOR EACH ROW EXECUTE FUNCTION public.security_audit_resource_delete();


--
-- Name: companies trg_security_audit_companies_webhook_insert; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_security_audit_companies_webhook_insert AFTER INSERT ON public.companies FOR EACH ROW WHEN (((new.webhook_url IS NOT NULL) AND (btrim(new.webhook_url) <> ''::text))) EXECUTE FUNCTION public.security_audit_companies_webhook_url();


--
-- Name: companies trg_security_audit_companies_webhook_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_security_audit_companies_webhook_update AFTER UPDATE OF webhook_url ON public.companies FOR EACH ROW WHEN ((old.webhook_url IS DISTINCT FROM new.webhook_url)) EXECUTE FUNCTION public.security_audit_companies_webhook_url();


--
-- Name: conversations trg_security_audit_conversations_delete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_security_audit_conversations_delete AFTER DELETE ON public.conversations FOR EACH ROW EXECUTE FUNCTION public.security_audit_resource_delete();


--
-- Name: documents trg_security_audit_documents_delete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_security_audit_documents_delete AFTER DELETE ON public.documents FOR EACH ROW EXECUTE FUNCTION public.security_audit_resource_delete();


--
-- Name: users_v2 trg_security_audit_users_v2_status; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_security_audit_users_v2_status AFTER UPDATE OF status ON public.users_v2 FOR EACH ROW WHEN (((old.status)::text IS DISTINCT FROM (new.status)::text)) EXECUTE FUNCTION public.security_audit_users_v2_status();


--
-- Name: agent_delegations trigger_delegations_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trigger_delegations_updated_at BEFORE UPDATE ON public.agent_delegations FOR EACH ROW EXECUTE FUNCTION public.update_agent_delegations_updated_at();


--
-- Name: documents trigger_documents_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trigger_documents_updated_at BEFORE UPDATE ON public.documents FOR EACH ROW EXECUTE FUNCTION public.update_documents_updated_at();


--
-- Name: ucp_connections ucp_connections_touch_config; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER ucp_connections_touch_config BEFORE UPDATE ON public.ucp_connections FOR EACH ROW EXECUTE FUNCTION public.ucp_connections_touch_config_updated_at();


--
-- Name: ucp_connections ucp_connections_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER ucp_connections_updated_at BEFORE UPDATE ON public.ucp_connections FOR EACH ROW EXECUTE FUNCTION public.update_ucp_updated_at();


--
-- Name: agent_mcp_tools update_agent_mcp_tools_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER update_agent_mcp_tools_updated_at BEFORE UPDATE ON public.agent_mcp_tools FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: agents update_agents_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER update_agents_updated_at BEFORE UPDATE ON public.agents FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: companies update_companies_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER update_companies_updated_at BEFORE UPDATE ON public.companies FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: legal_documents update_legal_documents_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER update_legal_documents_updated_at BEFORE UPDATE ON public.legal_documents FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: mcp_oauth_clients update_mcp_oauth_clients_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER update_mcp_oauth_clients_updated_at BEFORE UPDATE ON public.mcp_oauth_clients FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: plans update_plans_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER update_plans_updated_at BEFORE UPDATE ON public.plans FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: users_v2 update_users_v2_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER update_users_v2_updated_at BEFORE UPDATE ON public.users_v2 FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();


--
-- Name: agent_attendance_settings agent_attendance_settings_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_attendance_settings
    ADD CONSTRAINT agent_attendance_settings_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE CASCADE;


--
-- Name: agent_attendance_settings agent_attendance_settings_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_attendance_settings
    ADD CONSTRAINT agent_attendance_settings_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: agent_delegations agent_delegations_orchestrator_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_delegations
    ADD CONSTRAINT agent_delegations_orchestrator_id_fkey FOREIGN KEY (orchestrator_id) REFERENCES public.agents(id) ON DELETE CASCADE;


--
-- Name: agent_delegations agent_delegations_subagent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_delegations
    ADD CONSTRAINT agent_delegations_subagent_id_fkey FOREIGN KEY (subagent_id) REFERENCES public.agents(id) ON DELETE CASCADE;


--
-- Name: agent_http_tools agent_http_tools_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_http_tools
    ADD CONSTRAINT agent_http_tools_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE CASCADE;


--
-- Name: agent_mcp_connections agent_mcp_connections_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_mcp_connections
    ADD CONSTRAINT agent_mcp_connections_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE CASCADE;


--
-- Name: agent_mcp_connections agent_mcp_connections_mcp_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_mcp_connections
    ADD CONSTRAINT agent_mcp_connections_mcp_server_id_fkey FOREIGN KEY (mcp_server_id) REFERENCES public.mcp_servers(id) ON DELETE CASCADE;


--
-- Name: agent_mcp_tools agent_mcp_tools_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_mcp_tools
    ADD CONSTRAINT agent_mcp_tools_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE CASCADE;


--
-- Name: agent_mcp_tools agent_mcp_tools_mcp_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_mcp_tools
    ADD CONSTRAINT agent_mcp_tools_mcp_server_id_fkey FOREIGN KEY (mcp_server_id) REFERENCES public.mcp_servers(id) ON DELETE CASCADE;


--
-- Name: agents agents_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: attendance_sessions attendance_sessions_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_sessions
    ADD CONSTRAINT attendance_sessions_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: attendance_sessions attendance_sessions_closed_by_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_sessions
    ADD CONSTRAINT attendance_sessions_closed_by_agent_id_fkey FOREIGN KEY (closed_by_agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: attendance_sessions attendance_sessions_closed_by_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_sessions
    ADD CONSTRAINT attendance_sessions_closed_by_user_id_fkey FOREIGN KEY (closed_by_user_id) REFERENCES public.users_v2(id) ON DELETE SET NULL;


--
-- Name: attendance_sessions attendance_sessions_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_sessions
    ADD CONSTRAINT attendance_sessions_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: attendance_sessions attendance_sessions_conversation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_sessions
    ADD CONSTRAINT attendance_sessions_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES public.conversations(id) ON DELETE CASCADE;


--
-- Name: attendance_sessions attendance_sessions_human_requested_by_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_sessions
    ADD CONSTRAINT attendance_sessions_human_requested_by_agent_id_fkey FOREIGN KEY (human_requested_by_agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: attendance_sessions attendance_sessions_human_requested_by_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_sessions
    ADD CONSTRAINT attendance_sessions_human_requested_by_user_id_fkey FOREIGN KEY (human_requested_by_user_id) REFERENCES public.users_v2(id) ON DELETE SET NULL;


--
-- Name: attendance_sessions attendance_sessions_human_taken_by_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_sessions
    ADD CONSTRAINT attendance_sessions_human_taken_by_user_id_fkey FOREIGN KEY (human_taken_by_user_id) REFERENCES public.users_v2(id) ON DELETE SET NULL;


--
-- Name: attendance_sessions attendance_sessions_returned_to_ai_by_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_sessions
    ADD CONSTRAINT attendance_sessions_returned_to_ai_by_user_id_fkey FOREIGN KEY (returned_to_ai_by_user_id) REFERENCES public.users_v2(id) ON DELETE SET NULL;


--
-- Name: attendance_sla attendance_sla_attendance_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_sla
    ADD CONSTRAINT attendance_sla_attendance_session_id_fkey FOREIGN KEY (attendance_session_id) REFERENCES public.attendance_sessions(id) ON DELETE CASCADE;


--
-- Name: attendance_sla attendance_sla_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_sla
    ADD CONSTRAINT attendance_sla_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: attendance_sla attendance_sla_conversation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_sla
    ADD CONSTRAINT attendance_sla_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES public.conversations(id) ON DELETE CASCADE;


--
-- Name: attendance_sla attendance_sla_policy_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attendance_sla
    ADD CONSTRAINT attendance_sla_policy_id_fkey FOREIGN KEY (policy_id) REFERENCES public.sla_policies(id) ON DELETE SET NULL;


--
-- Name: company_attendance_settings company_attendance_settings_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_attendance_settings
    ADD CONSTRAINT company_attendance_settings_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: conversation_events conversation_events_actor_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_events
    ADD CONSTRAINT conversation_events_actor_agent_id_fkey FOREIGN KEY (actor_agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: conversation_events conversation_events_actor_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_events
    ADD CONSTRAINT conversation_events_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES public.users_v2(id) ON DELETE SET NULL;


--
-- Name: conversation_events conversation_events_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_events
    ADD CONSTRAINT conversation_events_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: conversation_events conversation_events_attendance_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_events
    ADD CONSTRAINT conversation_events_attendance_session_id_fkey FOREIGN KEY (attendance_session_id) REFERENCES public.attendance_sessions(id) ON DELETE SET NULL;


--
-- Name: conversation_events conversation_events_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_events
    ADD CONSTRAINT conversation_events_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: conversation_events conversation_events_conversation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_events
    ADD CONSTRAINT conversation_events_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES public.conversations(id) ON DELETE CASCADE;


--
-- Name: conversation_inactivity_timers conversation_inactivity_timers_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_inactivity_timers
    ADD CONSTRAINT conversation_inactivity_timers_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: conversation_inactivity_timers conversation_inactivity_timers_attendance_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_inactivity_timers
    ADD CONSTRAINT conversation_inactivity_timers_attendance_session_id_fkey FOREIGN KEY (attendance_session_id) REFERENCES public.attendance_sessions(id) ON DELETE CASCADE;


--
-- Name: conversation_inactivity_timers conversation_inactivity_timers_basis_message_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_inactivity_timers
    ADD CONSTRAINT conversation_inactivity_timers_basis_message_id_fkey FOREIGN KEY (basis_message_id) REFERENCES public.messages(id) ON DELETE SET NULL;


--
-- Name: conversation_inactivity_timers conversation_inactivity_timers_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_inactivity_timers
    ADD CONSTRAINT conversation_inactivity_timers_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: conversation_inactivity_timers conversation_inactivity_timers_conversation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_inactivity_timers
    ADD CONSTRAINT conversation_inactivity_timers_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES public.conversations(id) ON DELETE CASCADE;


--
-- Name: conversation_logs conversation_logs_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_logs
    ADD CONSTRAINT conversation_logs_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: conversation_logs conversation_logs_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation_logs
    ADD CONSTRAINT conversation_logs_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: conversations conversations_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT conversations_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: conversations conversations_assigned_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT conversations_assigned_user_id_fkey FOREIGN KEY (assigned_user_id) REFERENCES public.users_v2(id) ON DELETE SET NULL;


--
-- Name: conversations conversations_closed_by_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT conversations_closed_by_agent_id_fkey FOREIGN KEY (closed_by_agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: conversations conversations_closed_by_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT conversations_closed_by_user_id_fkey FOREIGN KEY (closed_by_user_id) REFERENCES public.users_v2(id) ON DELETE SET NULL;


--
-- Name: conversations conversations_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT conversations_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id);


--
-- Name: conversations conversations_current_attendance_session_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT conversations_current_attendance_session_fkey FOREIGN KEY (current_attendance_session_id) REFERENCES public.attendance_sessions(id) ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED;


--
-- Name: credit_transactions credit_transactions_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_transactions
    ADD CONSTRAINT credit_transactions_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: credit_transactions credit_transactions_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_transactions
    ADD CONSTRAINT credit_transactions_tenant_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: documents documents_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: documents documents_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: handoff_notification_recipients handoff_notification_recipients_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.handoff_notification_recipients
    ADD CONSTRAINT handoff_notification_recipients_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE CASCADE;


--
-- Name: handoff_notification_recipients handoff_notification_recipients_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.handoff_notification_recipients
    ADD CONSTRAINT handoff_notification_recipients_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: handoff_notification_recipients handoff_notification_recipients_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.handoff_notification_recipients
    ADD CONSTRAINT handoff_notification_recipients_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users_v2(id) ON DELETE SET NULL;


--
-- Name: integrations integrations_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.integrations
    ADD CONSTRAINT integrations_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: integrations integrations_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.integrations
    ADD CONSTRAINT integrations_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: internal_whatsapp_blocklist internal_whatsapp_blocklist_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.internal_whatsapp_blocklist
    ADD CONSTRAINT internal_whatsapp_blocklist_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE CASCADE;


--
-- Name: internal_whatsapp_blocklist internal_whatsapp_blocklist_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.internal_whatsapp_blocklist
    ADD CONSTRAINT internal_whatsapp_blocklist_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: internal_whatsapp_blocklist internal_whatsapp_blocklist_integration_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.internal_whatsapp_blocklist
    ADD CONSTRAINT internal_whatsapp_blocklist_integration_id_fkey FOREIGN KEY (integration_id) REFERENCES public.integrations(id) ON DELETE CASCADE;


--
-- Name: internal_whatsapp_blocklist internal_whatsapp_blocklist_source_recipient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.internal_whatsapp_blocklist
    ADD CONSTRAINT internal_whatsapp_blocklist_source_recipient_id_fkey FOREIGN KEY (source_recipient_id) REFERENCES public.handoff_notification_recipients(id) ON DELETE SET NULL;


--
-- Name: invites invites_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invites
    ADD CONSTRAINT invites_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: invites invites_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invites
    ADD CONSTRAINT invites_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users_v2(id);


--
-- Name: leads leads_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leads
    ADD CONSTRAINT leads_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: legal_documents legal_documents_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.legal_documents
    ADD CONSTRAINT legal_documents_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.admin_users(id);


--
-- Name: mcp_oauth_clients mcp_oauth_clients_mcp_server_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_oauth_clients
    ADD CONSTRAINT mcp_oauth_clients_mcp_server_id_fkey FOREIGN KEY (mcp_server_id) REFERENCES public.mcp_servers(id) ON DELETE CASCADE;


--
-- Name: memory_processing_locks memory_processing_locks_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_processing_locks
    ADD CONSTRAINT memory_processing_locks_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: memory_processing_locks memory_processing_locks_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_processing_locks
    ADD CONSTRAINT memory_processing_locks_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: memory_settings memory_settings_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_settings
    ADD CONSTRAINT memory_settings_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE CASCADE;


--
-- Name: memory_settings memory_settings_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_settings
    ADD CONSTRAINT memory_settings_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: messages messages_conversation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES public.conversations(id) ON DELETE CASCADE;


--
-- Name: messages messages_sender_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_sender_user_id_fkey FOREIGN KEY (sender_user_id) REFERENCES public.users_v2(id) ON DELETE SET NULL;


--
-- Name: notification_deliveries notification_deliveries_attendance_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_deliveries
    ADD CONSTRAINT notification_deliveries_attendance_session_id_fkey FOREIGN KEY (attendance_session_id) REFERENCES public.attendance_sessions(id) ON DELETE CASCADE;


--
-- Name: notification_deliveries notification_deliveries_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_deliveries
    ADD CONSTRAINT notification_deliveries_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: notification_deliveries notification_deliveries_conversation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_deliveries
    ADD CONSTRAINT notification_deliveries_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES public.conversations(id) ON DELETE CASCADE;


--
-- Name: notification_deliveries notification_deliveries_recipient_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_deliveries
    ADD CONSTRAINT notification_deliveries_recipient_id_fkey FOREIGN KEY (recipient_id) REFERENCES public.handoff_notification_recipients(id) ON DELETE SET NULL;


--
-- Name: password_reset_tokens password_reset_tokens_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users_v2(id) ON DELETE CASCADE;


--
-- Name: payment_history payment_history_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payment_history
    ADD CONSTRAINT payment_history_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users_v2(id) ON DELETE CASCADE;


--
-- Name: sanitization_jobs sanitization_jobs_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sanitization_jobs
    ADD CONSTRAINT sanitization_jobs_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id);


--
-- Name: session_summaries session_summaries_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_summaries
    ADD CONSTRAINT session_summaries_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: session_summaries session_summaries_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_summaries
    ADD CONSTRAINT session_summaries_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: sla_events sla_events_actor_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sla_events
    ADD CONSTRAINT sla_events_actor_agent_id_fkey FOREIGN KEY (actor_agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: sla_events sla_events_actor_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sla_events
    ADD CONSTRAINT sla_events_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES public.users_v2(id) ON DELETE SET NULL;


--
-- Name: sla_events sla_events_attendance_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sla_events
    ADD CONSTRAINT sla_events_attendance_session_id_fkey FOREIGN KEY (attendance_session_id) REFERENCES public.attendance_sessions(id) ON DELETE CASCADE;


--
-- Name: sla_events sla_events_attendance_sla_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sla_events
    ADD CONSTRAINT sla_events_attendance_sla_id_fkey FOREIGN KEY (attendance_sla_id) REFERENCES public.attendance_sla(id) ON DELETE CASCADE;


--
-- Name: sla_events sla_events_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sla_events
    ADD CONSTRAINT sla_events_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: sla_events sla_events_conversation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sla_events
    ADD CONSTRAINT sla_events_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES public.conversations(id) ON DELETE CASCADE;


--
-- Name: sla_policies sla_policies_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sla_policies
    ADD CONSTRAINT sla_policies_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: sla_policies sla_policies_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sla_policies
    ADD CONSTRAINT sla_policies_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users_v2(id) ON DELETE SET NULL;


--
-- Name: sla_policies sla_policies_updated_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sla_policies
    ADD CONSTRAINT sla_policies_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES public.users_v2(id) ON DELETE SET NULL;


--
-- Name: subscriptions subscriptions_plan_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_plan_id_fkey FOREIGN KEY (plan_id) REFERENCES public.plans(id) ON DELETE SET NULL;


--
-- Name: subscriptions subscriptions_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_tenant_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: system_logs system_logs_admin_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_logs
    ADD CONSTRAINT system_logs_admin_id_fkey FOREIGN KEY (admin_id) REFERENCES public.admin_users(id);


--
-- Name: system_logs system_logs_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_logs
    ADD CONSTRAINT system_logs_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id);


--
-- Name: system_logs system_logs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_logs
    ADD CONSTRAINT system_logs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users_v2(id);


--
-- Name: company_credits tenant_credits_tenant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.company_credits
    ADD CONSTRAINT tenant_credits_tenant_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: token_usage_logs token_usage_logs_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_usage_logs
    ADD CONSTRAINT token_usage_logs_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: token_usage_logs token_usage_logs_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_usage_logs
    ADD CONSTRAINT token_usage_logs_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE SET NULL;


--
-- Name: ucp_connections ucp_connections_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ucp_connections
    ADD CONSTRAINT ucp_connections_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE CASCADE;


--
-- Name: ucp_connections ucp_connections_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ucp_connections
    ADD CONSTRAINT ucp_connections_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: user_memories user_memories_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_memories
    ADD CONSTRAINT user_memories_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE SET NULL;


--
-- Name: user_memories user_memories_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_memories
    ADD CONSTRAINT user_memories_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id) ON DELETE CASCADE;


--
-- Name: users_v2 users_v2_accepted_terms_version_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users_v2
    ADD CONSTRAINT users_v2_accepted_terms_version_fkey FOREIGN KEY (accepted_terms_version) REFERENCES public.legal_documents(id);


--
-- Name: users_v2 users_v2_company_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users_v2
    ADD CONSTRAINT users_v2_company_id_fkey FOREIGN KEY (company_id) REFERENCES public.companies(id);


--
-- Name: users_v2 users_v2_plan_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users_v2
    ADD CONSTRAINT users_v2_plan_id_fkey FOREIGN KEY (plan_id) REFERENCES public.plans(id);


--
-- Name: widget_rate_limits widget_rate_limits_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.widget_rate_limits
    ADD CONSTRAINT widget_rate_limits_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE CASCADE;


--
-- Name: app_runtime_secrets; Type: ROW SECURITY; Schema: private; Owner: -
--

ALTER TABLE private.app_runtime_secrets ENABLE ROW LEVEL SECURITY;

--
-- Name: mcp_servers Anyone can read MCP servers; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Anyone can read MCP servers" ON public.mcp_servers FOR SELECT USING (true);


--
-- Name: llm_pricing Anyone can read pricing; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Anyone can read pricing" ON public.llm_pricing FOR SELECT USING (true);


--
-- Name: legal_documents Public read active legal_documents; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Public read active legal_documents" ON public.legal_documents FOR SELECT TO authenticated, anon USING ((is_active = true));


--
-- Name: token_usage_logs Service Role Full Access; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Service Role Full Access" ON public.token_usage_logs TO service_role USING (true) WITH CHECK (true);


--
-- Name: agent_mcp_connections Service role full access agent_mcp_connections; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Service role full access agent_mcp_connections" ON public.agent_mcp_connections TO service_role USING (true) WITH CHECK (true);


--
-- Name: agent_mcp_tools Service role full access agent_mcp_tools; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Service role full access agent_mcp_tools" ON public.agent_mcp_tools TO service_role USING (true) WITH CHECK (true);


--
-- Name: checkpoint_blobs Service role full access checkpoint_blobs; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Service role full access checkpoint_blobs" ON public.checkpoint_blobs TO service_role USING (true) WITH CHECK (true);


--
-- Name: checkpoint_migrations Service role full access checkpoint_migrations; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Service role full access checkpoint_migrations" ON public.checkpoint_migrations TO service_role USING (true) WITH CHECK (true);


--
-- Name: checkpoint_writes Service role full access checkpoint_writes; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Service role full access checkpoint_writes" ON public.checkpoint_writes TO service_role USING (true) WITH CHECK (true);


--
-- Name: checkpoints Service role full access checkpoints; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Service role full access checkpoints" ON public.checkpoints TO service_role USING (true) WITH CHECK (true);


--
-- Name: legal_documents Service role full access legal_documents; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Service role full access legal_documents" ON public.legal_documents TO service_role USING (true) WITH CHECK (true);


--
-- Name: mcp_servers Service role full access mcp_servers; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Service role full access mcp_servers" ON public.mcp_servers TO service_role USING (true) WITH CHECK (true);


--
-- Name: admin_users Service role full access to admin_users; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Service role full access to admin_users" ON public.admin_users TO service_role USING (true) WITH CHECK (true);


--
-- Name: agent_http_tools Service role full access to agent_http_tools; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Service role full access to agent_http_tools" ON public.agent_http_tools TO service_role USING (true) WITH CHECK (true);


--
-- Name: agents Service role full access to agents; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Service role full access to agents" ON public.agents TO service_role USING (true) WITH CHECK (true);


--
-- Name: companies Service role full access to companies; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Service role full access to companies" ON public.companies TO service_role USING (true) WITH CHECK (true);


--
-- Name: conversation_logs Service role full access to conversation_logs; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Service role full access to conversation_logs" ON public.conversation_logs TO service_role USING (true) WITH CHECK (true);


--
-- Name: conversations Service role full access to conversations; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Service role full access to conversations" ON public.conversations TO service_role USING (true) WITH CHECK (true);


--
-- Name: memory_processing_locks Service role full access to memory_processing_locks; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Service role full access to memory_processing_locks" ON public.memory_processing_locks TO service_role USING (true) WITH CHECK (true);


--
-- Name: messages Service role full access to messages; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Service role full access to messages" ON public.messages TO service_role USING (true) WITH CHECK (true);


--
-- Name: system_logs Service role full access to system_logs; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Service role full access to system_logs" ON public.system_logs TO service_role USING (true) WITH CHECK (true);


--
-- Name: users_v2 Service role full access to users_v2; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Service role full access to users_v2" ON public.users_v2 TO service_role USING (true) WITH CHECK (true);


--
-- Name: ucp_connections Service role full access ucp_connections; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Service role full access ucp_connections" ON public.ucp_connections TO service_role USING (true) WITH CHECK (true);


--
-- Name: conversations Users can create own conversations; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Users can create own conversations" ON public.conversations FOR INSERT WITH CHECK ((user_id = auth.uid()));


--
-- Name: messages Users can insert messages to own conversations; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Users can insert messages to own conversations" ON public.messages FOR INSERT WITH CHECK ((conversation_id IN ( SELECT conversations.id
   FROM public.conversations
  WHERE ((conversations.user_id = auth.uid()) OR (conversations.company_id = ( SELECT users_v2.company_id
           FROM public.users_v2
          WHERE (users_v2.id = auth.uid())))))));


--
-- Name: integrations Users can manage own company integrations; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Users can manage own company integrations" ON public.integrations USING ((company_id = ( SELECT users_v2.company_id
   FROM public.users_v2
  WHERE (users_v2.id = auth.uid()))));


--
-- Name: leads Users can manage own company leads; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Users can manage own company leads" ON public.leads USING ((company_id = ( SELECT users_v2.company_id
   FROM public.users_v2
  WHERE (users_v2.id = auth.uid()))));


--
-- Name: widget_rate_limits Users can manage rate limits for own company agents; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Users can manage rate limits for own company agents" ON public.widget_rate_limits USING ((agent_id IN ( SELECT agents.id
   FROM public.agents
  WHERE (agents.company_id = ( SELECT users_v2.company_id
           FROM public.users_v2
          WHERE (users_v2.id = auth.uid()))))));


--
-- Name: conversations Users can update own conversations; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Users can update own conversations" ON public.conversations FOR UPDATE USING ((user_id = auth.uid()));


--
-- Name: messages Users can view messages from accessible conversations; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Users can view messages from accessible conversations" ON public.messages FOR SELECT USING ((conversation_id IN ( SELECT conversations.id
   FROM public.conversations
  WHERE ((conversations.user_id = auth.uid()) OR (conversations.company_id = ( SELECT users_v2.company_id
           FROM public.users_v2
          WHERE (users_v2.id = auth.uid())))))));


--
-- Name: ucp_connections Users can view own company connections; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Users can view own company connections" ON public.ucp_connections FOR SELECT TO authenticated USING ((company_id IN ( SELECT users_v2.company_id
   FROM public.users_v2
  WHERE (users_v2.id = auth.uid()))));


--
-- Name: company_credits Users can view own company credits; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Users can view own company credits" ON public.company_credits FOR SELECT USING ((company_id = ( SELECT users_v2.company_id
   FROM public.users_v2
  WHERE (users_v2.id = auth.uid()))));


--
-- Name: subscriptions Users can view own company subscription; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Users can view own company subscription" ON public.subscriptions FOR SELECT USING ((company_id = ( SELECT users_v2.company_id
   FROM public.users_v2
  WHERE (users_v2.id = auth.uid()))));


--
-- Name: credit_transactions Users can view own company transactions; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Users can view own company transactions" ON public.credit_transactions FOR SELECT USING ((company_id = ( SELECT users_v2.company_id
   FROM public.users_v2
  WHERE (users_v2.id = auth.uid()))));


--
-- Name: conversations Users can view own or company conversations; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Users can view own or company conversations" ON public.conversations FOR SELECT USING (((user_id = auth.uid()) OR (company_id = ( SELECT users_v2.company_id
   FROM public.users_v2
  WHERE (users_v2.id = auth.uid())))));


--
-- Name: admin_users; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.admin_users ENABLE ROW LEVEL SECURITY;

--
-- Name: agent_attendance_settings; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.agent_attendance_settings ENABLE ROW LEVEL SECURITY;

--
-- Name: agent_attendance_settings agent_attendance_settings_admin_scope; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY agent_attendance_settings_admin_scope ON public.agent_attendance_settings FOR SELECT TO authenticated USING (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND ((auth.jwt() ->> 'role'::text) = ANY (ARRAY['admin'::text, 'company_admin'::text, 'master_admin'::text]))));


--
-- Name: agent_delegations; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.agent_delegations ENABLE ROW LEVEL SECURITY;

--
-- Name: agent_http_tools; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.agent_http_tools ENABLE ROW LEVEL SECURITY;

--
-- Name: agent_mcp_connections; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.agent_mcp_connections ENABLE ROW LEVEL SECURITY;

--
-- Name: agent_mcp_tools; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.agent_mcp_tools ENABLE ROW LEVEL SECURITY;

--
-- Name: agents; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.agents ENABLE ROW LEVEL SECURITY;

--
-- Name: attendance_sessions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.attendance_sessions ENABLE ROW LEVEL SECURITY;

--
-- Name: attendance_sessions attendance_sessions_company_scope; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY attendance_sessions_company_scope ON public.attendance_sessions FOR SELECT TO authenticated USING (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (((auth.jwt() ->> 'role'::text) = ANY (ARRAY['admin'::text, 'company_admin'::text, 'master_admin'::text])) OR (EXISTS ( SELECT 1
   FROM public.conversations c
  WHERE ((c.id = attendance_sessions.conversation_id) AND (c.company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (c.assigned_user_id = ((auth.jwt() ->> 'user_id'::text))::uuid)))))));


--
-- Name: attendance_sla; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.attendance_sla ENABLE ROW LEVEL SECURITY;

--
-- Name: attendance_sla attendance_sla_company_scope; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY attendance_sla_company_scope ON public.attendance_sla FOR SELECT TO authenticated USING (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (((auth.jwt() ->> 'role'::text) = ANY (ARRAY['admin'::text, 'company_admin'::text, 'master_admin'::text])) OR (EXISTS ( SELECT 1
   FROM public.conversations c
  WHERE ((c.id = attendance_sla.conversation_id) AND (c.company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (c.assigned_user_id = ((auth.jwt() ->> 'user_id'::text))::uuid)))))));


--
-- Name: checkpoint_blobs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.checkpoint_blobs ENABLE ROW LEVEL SECURITY;

--
-- Name: checkpoint_migrations; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.checkpoint_migrations ENABLE ROW LEVEL SECURITY;

--
-- Name: checkpoint_writes; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.checkpoint_writes ENABLE ROW LEVEL SECURITY;

--
-- Name: checkpoints; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.checkpoints ENABLE ROW LEVEL SECURITY;

--
-- Name: companies; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.companies ENABLE ROW LEVEL SECURITY;

--
-- Name: company_attendance_settings; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.company_attendance_settings ENABLE ROW LEVEL SECURITY;

--
-- Name: company_attendance_settings company_attendance_settings_admin_scope; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY company_attendance_settings_admin_scope ON public.company_attendance_settings FOR SELECT TO authenticated USING (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND ((auth.jwt() ->> 'role'::text) = ANY (ARRAY['admin'::text, 'company_admin'::text, 'master_admin'::text]))));


--
-- Name: company_credits; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.company_credits ENABLE ROW LEVEL SECURITY;

--
-- Name: sanitization_jobs company_isolation_delete; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY company_isolation_delete ON public.sanitization_jobs FOR DELETE USING ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: sanitization_jobs company_isolation_insert; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY company_isolation_insert ON public.sanitization_jobs FOR INSERT WITH CHECK ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: sanitization_jobs company_isolation_select; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY company_isolation_select ON public.sanitization_jobs FOR SELECT USING ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: conversation_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.conversation_events ENABLE ROW LEVEL SECURITY;

--
-- Name: conversation_events conversation_events_company_scope; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY conversation_events_company_scope ON public.conversation_events FOR SELECT TO authenticated USING (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (((auth.jwt() ->> 'role'::text) = ANY (ARRAY['admin'::text, 'company_admin'::text, 'master_admin'::text])) OR (EXISTS ( SELECT 1
   FROM public.conversations c
  WHERE ((c.id = conversation_events.conversation_id) AND (c.company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (c.assigned_user_id = ((auth.jwt() ->> 'user_id'::text))::uuid)))))));


--
-- Name: conversation_inactivity_timers; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.conversation_inactivity_timers ENABLE ROW LEVEL SECURITY;

--
-- Name: conversation_inactivity_timers conversation_inactivity_timers_company_scope; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY conversation_inactivity_timers_company_scope ON public.conversation_inactivity_timers FOR SELECT TO authenticated USING (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (((auth.jwt() ->> 'role'::text) = ANY (ARRAY['admin'::text, 'company_admin'::text, 'master_admin'::text])) OR (EXISTS ( SELECT 1
   FROM public.conversations c
  WHERE ((c.id = conversation_inactivity_timers.conversation_id) AND (c.company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (c.assigned_user_id = ((auth.jwt() ->> 'user_id'::text))::uuid)))))));


--
-- Name: conversation_logs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.conversation_logs ENABLE ROW LEVEL SECURITY;

--
-- Name: conversations; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.conversations ENABLE ROW LEVEL SECURITY;

--
-- Name: credit_transactions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.credit_transactions ENABLE ROW LEVEL SECURITY;

--
-- Name: agent_delegations delegations_same_company; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY delegations_same_company ON public.agent_delegations TO authenticated USING (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (EXISTS ( SELECT 1
   FROM (public.agents orchestrator
     JOIN public.agents subagent ON ((subagent.id = agent_delegations.subagent_id)))
  WHERE ((orchestrator.id = agent_delegations.orchestrator_id) AND (orchestrator.company_id = agent_delegations.company_id) AND (subagent.company_id = agent_delegations.company_id)))))) WITH CHECK (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (EXISTS ( SELECT 1
   FROM (public.agents orchestrator
     JOIN public.agents subagent ON ((subagent.id = agent_delegations.subagent_id)))
  WHERE ((orchestrator.id = agent_delegations.orchestrator_id) AND (orchestrator.company_id = agent_delegations.company_id) AND (subagent.company_id = agent_delegations.company_id))))));


--
-- Name: documents; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.documents ENABLE ROW LEVEL SECURITY;

--
-- Name: handoff_notification_recipients; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.handoff_notification_recipients ENABLE ROW LEVEL SECURITY;

--
-- Name: handoff_notification_recipients handoff_notification_recipients_admin_scope; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY handoff_notification_recipients_admin_scope ON public.handoff_notification_recipients FOR SELECT TO authenticated USING (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND ((auth.jwt() ->> 'role'::text) = ANY (ARRAY['admin'::text, 'company_admin'::text, 'master_admin'::text]))));


--
-- Name: integrations; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.integrations ENABLE ROW LEVEL SECURITY;

--
-- Name: internal_whatsapp_blocklist; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.internal_whatsapp_blocklist ENABLE ROW LEVEL SECURITY;

--
-- Name: internal_whatsapp_blocklist internal_whatsapp_blocklist_admin_scope; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY internal_whatsapp_blocklist_admin_scope ON public.internal_whatsapp_blocklist FOR SELECT TO authenticated USING (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND ((auth.jwt() ->> 'role'::text) = ANY (ARRAY['admin'::text, 'company_admin'::text, 'master_admin'::text]))));


--
-- Name: invites; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.invites ENABLE ROW LEVEL SECURITY;

--
-- Name: leads; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.leads ENABLE ROW LEVEL SECURITY;

--
-- Name: legal_documents; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.legal_documents ENABLE ROW LEVEL SECURITY;

--
-- Name: llm_pricing; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.llm_pricing ENABLE ROW LEVEL SECURITY;

--
-- Name: mcp_oauth_clients; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.mcp_oauth_clients ENABLE ROW LEVEL SECURITY;

--
-- Name: mcp_servers; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.mcp_servers ENABLE ROW LEVEL SECURITY;

--
-- Name: memory_processing_locks; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.memory_processing_locks ENABLE ROW LEVEL SECURITY;

--
-- Name: memory_settings; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.memory_settings ENABLE ROW LEVEL SECURITY;

--
-- Name: messages; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.messages ENABLE ROW LEVEL SECURITY;

--
-- Name: notification_deliveries; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.notification_deliveries ENABLE ROW LEVEL SECURITY;

--
-- Name: notification_deliveries notification_deliveries_company_scope; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY notification_deliveries_company_scope ON public.notification_deliveries FOR SELECT TO authenticated USING (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (((auth.jwt() ->> 'role'::text) = ANY (ARRAY['admin'::text, 'company_admin'::text, 'master_admin'::text])) OR (EXISTS ( SELECT 1
   FROM public.conversations c
  WHERE ((c.id = notification_deliveries.conversation_id) AND (c.company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (c.assigned_user_id = ((auth.jwt() ->> 'user_id'::text))::uuid)))))));


--
-- Name: password_reset_tokens; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.password_reset_tokens ENABLE ROW LEVEL SECURITY;

--
-- Name: payment_history; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.payment_history ENABLE ROW LEVEL SECURITY;

--
-- Name: plans; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.plans ENABLE ROW LEVEL SECURITY;

--
-- Name: platform_provider_alerts; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.platform_provider_alerts ENABLE ROW LEVEL SECURITY;

--
-- Name: platform_settings; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.platform_settings ENABLE ROW LEVEL SECURITY;

--
-- Name: sanitization_jobs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.sanitization_jobs ENABLE ROW LEVEL SECURITY;

--
-- Name: agent_http_tools service_role_all; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY service_role_all ON public.agent_http_tools TO service_role USING (true) WITH CHECK (true);


--
-- Name: session_summaries; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.session_summaries ENABLE ROW LEVEL SECURITY;

--
-- Name: sla_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.sla_events ENABLE ROW LEVEL SECURITY;

--
-- Name: sla_events sla_events_company_scope; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY sla_events_company_scope ON public.sla_events FOR SELECT TO authenticated USING (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (((auth.jwt() ->> 'role'::text) = ANY (ARRAY['admin'::text, 'company_admin'::text, 'master_admin'::text])) OR (EXISTS ( SELECT 1
   FROM public.conversations c
  WHERE ((c.id = sla_events.conversation_id) AND (c.company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (c.assigned_user_id = ((auth.jwt() ->> 'user_id'::text))::uuid)))))));


--
-- Name: sla_policies; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.sla_policies ENABLE ROW LEVEL SECURITY;

--
-- Name: sla_policies sla_policies_admin_scope; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY sla_policies_admin_scope ON public.sla_policies FOR SELECT TO authenticated USING (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND ((auth.jwt() ->> 'role'::text) = ANY (ARRAY['admin'::text, 'company_admin'::text, 'master_admin'::text]))));


--
-- Name: subscriptions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.subscriptions ENABLE ROW LEVEL SECURITY;

--
-- Name: system_logs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.system_logs ENABLE ROW LEVEL SECURITY;

--
-- Name: admin_users tenant_delete_admin_users; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_delete_admin_users ON public.admin_users FOR DELETE TO authenticated, anon USING ((((auth.jwt() ->> 'role'::text) = 'master_admin'::text) OR (((auth.jwt() ->> 'role'::text) = 'company_admin'::text) AND (role = 'company_admin'::text) AND (company_id = ((auth.jwt() ->> 'company_id'::text))::uuid))));


--
-- Name: agent_http_tools tenant_delete_agent_http_tools; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_delete_agent_http_tools ON public.agent_http_tools FOR DELETE TO authenticated, anon USING (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (EXISTS ( SELECT 1
   FROM public.agents
  WHERE ((agents.id = agent_http_tools.agent_id) AND (agents.company_id = agent_http_tools.company_id))))));


--
-- Name: agent_mcp_connections tenant_delete_agent_mcp_connections; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_delete_agent_mcp_connections ON public.agent_mcp_connections FOR DELETE TO authenticated, anon USING (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (EXISTS ( SELECT 1
   FROM public.agents
  WHERE ((agents.id = agent_mcp_connections.agent_id) AND (agents.company_id = agent_mcp_connections.company_id))))));


--
-- Name: agent_mcp_tools tenant_delete_agent_mcp_tools; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_delete_agent_mcp_tools ON public.agent_mcp_tools FOR DELETE TO authenticated, anon USING (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (EXISTS ( SELECT 1
   FROM public.agents
  WHERE ((agents.id = agent_mcp_tools.agent_id) AND (agents.company_id = agent_mcp_tools.company_id))))));


--
-- Name: agents tenant_delete_agents; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_delete_agents ON public.agents FOR DELETE TO authenticated, anon USING ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: checkpoints tenant_delete_checkpoints; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_delete_checkpoints ON public.checkpoints FOR DELETE TO authenticated, anon USING ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: companies tenant_delete_companies; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_delete_companies ON public.companies FOR DELETE TO authenticated, anon USING ((id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: conversation_logs tenant_delete_conversation_logs; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_delete_conversation_logs ON public.conversation_logs FOR DELETE TO authenticated, anon USING ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: documents tenant_delete_documents; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_delete_documents ON public.documents FOR DELETE TO authenticated, anon USING ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: memory_processing_locks tenant_delete_memory_processing_locks; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_delete_memory_processing_locks ON public.memory_processing_locks FOR DELETE TO authenticated, anon USING ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: system_logs tenant_delete_system_logs; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_delete_system_logs ON public.system_logs FOR DELETE TO authenticated, anon USING ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: users_v2 tenant_delete_users_v2; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_delete_users_v2 ON public.users_v2 FOR DELETE TO authenticated, anon USING ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: admin_users tenant_insert_admin_users; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_insert_admin_users ON public.admin_users FOR INSERT TO authenticated, anon WITH CHECK ((((auth.jwt() ->> 'role'::text) = 'master_admin'::text) OR (((auth.jwt() ->> 'role'::text) = 'company_admin'::text) AND (role = 'company_admin'::text) AND (company_id = ((auth.jwt() ->> 'company_id'::text))::uuid))));


--
-- Name: agent_http_tools tenant_insert_agent_http_tools; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_insert_agent_http_tools ON public.agent_http_tools FOR INSERT TO authenticated, anon WITH CHECK (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (EXISTS ( SELECT 1
   FROM public.agents
  WHERE ((agents.id = agent_http_tools.agent_id) AND (agents.company_id = agent_http_tools.company_id))))));


--
-- Name: agent_mcp_connections tenant_insert_agent_mcp_connections; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_insert_agent_mcp_connections ON public.agent_mcp_connections FOR INSERT TO authenticated, anon WITH CHECK (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (EXISTS ( SELECT 1
   FROM public.agents
  WHERE ((agents.id = agent_mcp_connections.agent_id) AND (agents.company_id = agent_mcp_connections.company_id))))));


--
-- Name: agent_mcp_tools tenant_insert_agent_mcp_tools; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_insert_agent_mcp_tools ON public.agent_mcp_tools FOR INSERT TO authenticated, anon WITH CHECK (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (EXISTS ( SELECT 1
   FROM public.agents
  WHERE ((agents.id = agent_mcp_tools.agent_id) AND (agents.company_id = agent_mcp_tools.company_id))))));


--
-- Name: agents tenant_insert_agents; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_insert_agents ON public.agents FOR INSERT TO authenticated, anon WITH CHECK ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: checkpoints tenant_insert_checkpoints; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_insert_checkpoints ON public.checkpoints FOR INSERT TO authenticated, anon WITH CHECK ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: companies tenant_insert_companies; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_insert_companies ON public.companies FOR INSERT TO authenticated, anon WITH CHECK ((id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: conversation_logs tenant_insert_conversation_logs; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_insert_conversation_logs ON public.conversation_logs FOR INSERT TO authenticated, anon WITH CHECK ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: documents tenant_insert_documents; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_insert_documents ON public.documents FOR INSERT TO authenticated, anon WITH CHECK ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: memory_processing_locks tenant_insert_memory_processing_locks; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_insert_memory_processing_locks ON public.memory_processing_locks FOR INSERT TO authenticated, anon WITH CHECK ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: system_logs tenant_insert_system_logs; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_insert_system_logs ON public.system_logs FOR INSERT TO authenticated, anon WITH CHECK ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: users_v2 tenant_insert_users_v2; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_insert_users_v2 ON public.users_v2 FOR INSERT TO authenticated, anon WITH CHECK ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: admin_users tenant_select_admin_users; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_select_admin_users ON public.admin_users FOR SELECT TO authenticated, anon USING ((((auth.jwt() ->> 'role'::text) = 'master_admin'::text) OR (((auth.jwt() ->> 'role'::text) = 'company_admin'::text) AND (role = 'company_admin'::text) AND (company_id = ((auth.jwt() ->> 'company_id'::text))::uuid))));


--
-- Name: agent_http_tools tenant_select_agent_http_tools; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_select_agent_http_tools ON public.agent_http_tools FOR SELECT TO authenticated, anon USING (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (EXISTS ( SELECT 1
   FROM public.agents
  WHERE ((agents.id = agent_http_tools.agent_id) AND (agents.company_id = agent_http_tools.company_id))))));


--
-- Name: agent_mcp_connections tenant_select_agent_mcp_connections; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_select_agent_mcp_connections ON public.agent_mcp_connections FOR SELECT TO authenticated, anon USING (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (EXISTS ( SELECT 1
   FROM public.agents
  WHERE ((agents.id = agent_mcp_connections.agent_id) AND (agents.company_id = agent_mcp_connections.company_id))))));


--
-- Name: agent_mcp_tools tenant_select_agent_mcp_tools; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_select_agent_mcp_tools ON public.agent_mcp_tools FOR SELECT TO authenticated, anon USING (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (EXISTS ( SELECT 1
   FROM public.agents
  WHERE ((agents.id = agent_mcp_tools.agent_id) AND (agents.company_id = agent_mcp_tools.company_id))))));


--
-- Name: agents tenant_select_agents; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_select_agents ON public.agents FOR SELECT TO authenticated, anon USING ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: checkpoints tenant_select_checkpoints; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_select_checkpoints ON public.checkpoints FOR SELECT TO authenticated, anon USING ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: companies tenant_select_companies; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_select_companies ON public.companies FOR SELECT TO authenticated, anon USING ((id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: conversation_logs tenant_select_conversation_logs; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_select_conversation_logs ON public.conversation_logs FOR SELECT TO authenticated, anon USING ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: documents tenant_select_documents; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_select_documents ON public.documents FOR SELECT TO authenticated, anon USING ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: memory_processing_locks tenant_select_memory_processing_locks; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_select_memory_processing_locks ON public.memory_processing_locks FOR SELECT TO authenticated, anon USING ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: system_logs tenant_select_system_logs; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_select_system_logs ON public.system_logs FOR SELECT TO authenticated, anon USING ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: users_v2 tenant_select_users_v2; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_select_users_v2 ON public.users_v2 FOR SELECT TO authenticated, anon USING ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: admin_users tenant_update_admin_users; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_update_admin_users ON public.admin_users FOR UPDATE TO authenticated, anon USING ((((auth.jwt() ->> 'role'::text) = 'master_admin'::text) OR (((auth.jwt() ->> 'role'::text) = 'company_admin'::text) AND (role = 'company_admin'::text) AND (company_id = ((auth.jwt() ->> 'company_id'::text))::uuid)))) WITH CHECK ((((auth.jwt() ->> 'role'::text) = 'master_admin'::text) OR (((auth.jwt() ->> 'role'::text) = 'company_admin'::text) AND (role = 'company_admin'::text) AND (company_id = ((auth.jwt() ->> 'company_id'::text))::uuid))));


--
-- Name: agent_http_tools tenant_update_agent_http_tools; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_update_agent_http_tools ON public.agent_http_tools FOR UPDATE TO authenticated, anon USING (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (EXISTS ( SELECT 1
   FROM public.agents
  WHERE ((agents.id = agent_http_tools.agent_id) AND (agents.company_id = agent_http_tools.company_id)))))) WITH CHECK (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (EXISTS ( SELECT 1
   FROM public.agents
  WHERE ((agents.id = agent_http_tools.agent_id) AND (agents.company_id = agent_http_tools.company_id))))));


--
-- Name: agent_mcp_connections tenant_update_agent_mcp_connections; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_update_agent_mcp_connections ON public.agent_mcp_connections FOR UPDATE TO authenticated, anon USING (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (EXISTS ( SELECT 1
   FROM public.agents
  WHERE ((agents.id = agent_mcp_connections.agent_id) AND (agents.company_id = agent_mcp_connections.company_id)))))) WITH CHECK (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (EXISTS ( SELECT 1
   FROM public.agents
  WHERE ((agents.id = agent_mcp_connections.agent_id) AND (agents.company_id = agent_mcp_connections.company_id))))));


--
-- Name: agent_mcp_tools tenant_update_agent_mcp_tools; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_update_agent_mcp_tools ON public.agent_mcp_tools FOR UPDATE TO authenticated, anon USING (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (EXISTS ( SELECT 1
   FROM public.agents
  WHERE ((agents.id = agent_mcp_tools.agent_id) AND (agents.company_id = agent_mcp_tools.company_id)))))) WITH CHECK (((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid) AND (EXISTS ( SELECT 1
   FROM public.agents
  WHERE ((agents.id = agent_mcp_tools.agent_id) AND (agents.company_id = agent_mcp_tools.company_id))))));


--
-- Name: agents tenant_update_agents; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_update_agents ON public.agents FOR UPDATE TO authenticated, anon USING ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid)) WITH CHECK ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: checkpoints tenant_update_checkpoints; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_update_checkpoints ON public.checkpoints FOR UPDATE TO authenticated, anon USING ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid)) WITH CHECK ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: companies tenant_update_companies; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_update_companies ON public.companies FOR UPDATE TO authenticated, anon USING ((id = ((auth.jwt() ->> 'company_id'::text))::uuid)) WITH CHECK ((id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: conversation_logs tenant_update_conversation_logs; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_update_conversation_logs ON public.conversation_logs FOR UPDATE TO authenticated, anon USING ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid)) WITH CHECK ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: documents tenant_update_documents; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_update_documents ON public.documents FOR UPDATE TO authenticated, anon USING ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid)) WITH CHECK ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: memory_processing_locks tenant_update_memory_processing_locks; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_update_memory_processing_locks ON public.memory_processing_locks FOR UPDATE TO authenticated, anon USING ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid)) WITH CHECK ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: system_logs tenant_update_system_logs; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_update_system_logs ON public.system_logs FOR UPDATE TO authenticated, anon USING ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid)) WITH CHECK ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: users_v2 tenant_update_users_v2; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY tenant_update_users_v2 ON public.users_v2 FOR UPDATE TO authenticated, anon USING ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid)) WITH CHECK ((company_id = ((auth.jwt() ->> 'company_id'::text))::uuid));


--
-- Name: token_usage_logs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.token_usage_logs ENABLE ROW LEVEL SECURITY;

--
-- Name: token_usage_outbox; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.token_usage_outbox ENABLE ROW LEVEL SECURITY;

--
-- Name: ucp_connections; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.ucp_connections ENABLE ROW LEVEL SECURITY;

--
-- Name: user_memories; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.user_memories ENABLE ROW LEVEL SECURITY;

--
-- Name: users_v2; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.users_v2 ENABLE ROW LEVEL SECURITY;

--
-- Name: widget_rate_limits; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.widget_rate_limits ENABLE ROW LEVEL SECURITY;

--
-- Name: SCHEMA public; Type: ACL; Schema: -; Owner: -
--

GRANT USAGE ON SCHEMA public TO postgres;
GRANT USAGE ON SCHEMA public TO anon;
GRANT USAGE ON SCHEMA public TO authenticated;
GRANT USAGE ON SCHEMA public TO service_role;


--
-- Name: FUNCTION _attendance_create_sla(p_attendance_session_id uuid, p_conversation_id uuid, p_company_id uuid, p_sla_level text, p_started_at timestamp with time zone, p_first_response_deadline timestamp with time zone, p_resolution_deadline timestamp with time zone, p_policy_snapshot jsonb); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public._attendance_create_sla(p_attendance_session_id uuid, p_conversation_id uuid, p_company_id uuid, p_sla_level text, p_started_at timestamp with time zone, p_first_response_deadline timestamp with time zone, p_resolution_deadline timestamp with time zone, p_policy_snapshot jsonb) FROM PUBLIC;
GRANT ALL ON FUNCTION public._attendance_create_sla(p_attendance_session_id uuid, p_conversation_id uuid, p_company_id uuid, p_sla_level text, p_started_at timestamp with time zone, p_first_response_deadline timestamp with time zone, p_resolution_deadline timestamp with time zone, p_policy_snapshot jsonb) TO anon;
GRANT ALL ON FUNCTION public._attendance_create_sla(p_attendance_session_id uuid, p_conversation_id uuid, p_company_id uuid, p_sla_level text, p_started_at timestamp with time zone, p_first_response_deadline timestamp with time zone, p_resolution_deadline timestamp with time zone, p_policy_snapshot jsonb) TO authenticated;
GRANT ALL ON FUNCTION public._attendance_create_sla(p_attendance_session_id uuid, p_conversation_id uuid, p_company_id uuid, p_sla_level text, p_started_at timestamp with time zone, p_first_response_deadline timestamp with time zone, p_resolution_deadline timestamp with time zone, p_policy_snapshot jsonb) TO service_role;


--
-- Name: FUNCTION _attendance_enqueue_handoff_notifications(p_conversation_id uuid, p_attendance_session_id uuid, p_company_id uuid, p_agent_id uuid); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public._attendance_enqueue_handoff_notifications(p_conversation_id uuid, p_attendance_session_id uuid, p_company_id uuid, p_agent_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public._attendance_enqueue_handoff_notifications(p_conversation_id uuid, p_attendance_session_id uuid, p_company_id uuid, p_agent_id uuid) TO anon;
GRANT ALL ON FUNCTION public._attendance_enqueue_handoff_notifications(p_conversation_id uuid, p_attendance_session_id uuid, p_company_id uuid, p_agent_id uuid) TO authenticated;
GRANT ALL ON FUNCTION public._attendance_enqueue_handoff_notifications(p_conversation_id uuid, p_attendance_session_id uuid, p_company_id uuid, p_agent_id uuid) TO service_role;


--
-- Name: FUNCTION _attendance_ensure_open_session(p_conversation_id uuid, p_company_id uuid, p_agent_id uuid, p_status text, p_payload jsonb, p_actor_type text, p_actor_agent_id uuid, p_actor_user_id uuid); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public._attendance_ensure_open_session(p_conversation_id uuid, p_company_id uuid, p_agent_id uuid, p_status text, p_payload jsonb, p_actor_type text, p_actor_agent_id uuid, p_actor_user_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public._attendance_ensure_open_session(p_conversation_id uuid, p_company_id uuid, p_agent_id uuid, p_status text, p_payload jsonb, p_actor_type text, p_actor_agent_id uuid, p_actor_user_id uuid) TO anon;
GRANT ALL ON FUNCTION public._attendance_ensure_open_session(p_conversation_id uuid, p_company_id uuid, p_agent_id uuid, p_status text, p_payload jsonb, p_actor_type text, p_actor_agent_id uuid, p_actor_user_id uuid) TO authenticated;
GRANT ALL ON FUNCTION public._attendance_ensure_open_session(p_conversation_id uuid, p_company_id uuid, p_agent_id uuid, p_status text, p_payload jsonb, p_actor_type text, p_actor_agent_id uuid, p_actor_user_id uuid) TO service_role;


--
-- Name: FUNCTION _attendance_record_event(p_conversation_id uuid, p_attendance_session_id uuid, p_company_id uuid, p_agent_id uuid, p_event_type text, p_actor_type text, p_actor_user_id uuid, p_actor_agent_id uuid, p_metadata jsonb, p_idempotency_key text); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public._attendance_record_event(p_conversation_id uuid, p_attendance_session_id uuid, p_company_id uuid, p_agent_id uuid, p_event_type text, p_actor_type text, p_actor_user_id uuid, p_actor_agent_id uuid, p_metadata jsonb, p_idempotency_key text) FROM PUBLIC;
GRANT ALL ON FUNCTION public._attendance_record_event(p_conversation_id uuid, p_attendance_session_id uuid, p_company_id uuid, p_agent_id uuid, p_event_type text, p_actor_type text, p_actor_user_id uuid, p_actor_agent_id uuid, p_metadata jsonb, p_idempotency_key text) TO anon;
GRANT ALL ON FUNCTION public._attendance_record_event(p_conversation_id uuid, p_attendance_session_id uuid, p_company_id uuid, p_agent_id uuid, p_event_type text, p_actor_type text, p_actor_user_id uuid, p_actor_agent_id uuid, p_metadata jsonb, p_idempotency_key text) TO authenticated;
GRANT ALL ON FUNCTION public._attendance_record_event(p_conversation_id uuid, p_attendance_session_id uuid, p_company_id uuid, p_agent_id uuid, p_event_type text, p_actor_type text, p_actor_user_id uuid, p_actor_agent_id uuid, p_metadata jsonb, p_idempotency_key text) TO service_role;


--
-- Name: FUNCTION agent_mcp_connections_touch_config_updated_at(); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.agent_mcp_connections_touch_config_updated_at() TO anon;
GRANT ALL ON FUNCTION public.agent_mcp_connections_touch_config_updated_at() TO authenticated;
GRANT ALL ON FUNCTION public.agent_mcp_connections_touch_config_updated_at() TO service_role;


--
-- Name: FUNCTION bill_usage_group(p_log_ids uuid[], p_company_id uuid, p_agent_id uuid, p_model_name text, p_dollar_rate numeric); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.bill_usage_group(p_log_ids uuid[], p_company_id uuid, p_agent_id uuid, p_model_name text, p_dollar_rate numeric) FROM PUBLIC;
GRANT ALL ON FUNCTION public.bill_usage_group(p_log_ids uuid[], p_company_id uuid, p_agent_id uuid, p_model_name text, p_dollar_rate numeric) TO service_role;


--
-- Name: FUNCTION check_and_increment_rate_limit(p_identifier text, p_agent_id uuid, p_max_requests integer, p_window_minutes integer); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.check_and_increment_rate_limit(p_identifier text, p_agent_id uuid, p_max_requests integer, p_window_minutes integer) TO anon;
GRANT ALL ON FUNCTION public.check_and_increment_rate_limit(p_identifier text, p_agent_id uuid, p_max_requests integer, p_window_minutes integer) TO authenticated;
GRANT ALL ON FUNCTION public.check_and_increment_rate_limit(p_identifier text, p_agent_id uuid, p_max_requests integer, p_window_minutes integer) TO service_role;


--
-- Name: FUNCTION create_user_account(p_first_name character varying, p_last_name character varying, p_email character varying, p_password_hash character varying, p_cpf character varying, p_phone character varying, p_birth_date date, p_company_id uuid, p_status character varying, p_role character varying, p_is_owner boolean); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.create_user_account(p_first_name character varying, p_last_name character varying, p_email character varying, p_password_hash character varying, p_cpf character varying, p_phone character varying, p_birth_date date, p_company_id uuid, p_status character varying, p_role character varying, p_is_owner boolean) TO anon;
GRANT ALL ON FUNCTION public.create_user_account(p_first_name character varying, p_last_name character varying, p_email character varying, p_password_hash character varying, p_cpf character varying, p_phone character varying, p_birth_date date, p_company_id uuid, p_status character varying, p_role character varying, p_is_owner boolean) TO authenticated;
GRANT ALL ON FUNCTION public.create_user_account(p_first_name character varying, p_last_name character varying, p_email character varying, p_password_hash character varying, p_cpf character varying, p_phone character varying, p_birth_date date, p_company_id uuid, p_status character varying, p_role character varying, p_is_owner boolean) TO service_role;


--
-- Name: FUNCTION create_user_account(p_first_name character varying, p_last_name character varying, p_email character varying, p_password_hash character varying, p_cpf character varying, p_phone character varying, p_birth_date date, p_company_id uuid, p_status character varying, p_role character varying, p_is_owner boolean, p_accepted_terms_version uuid); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.create_user_account(p_first_name character varying, p_last_name character varying, p_email character varying, p_password_hash character varying, p_cpf character varying, p_phone character varying, p_birth_date date, p_company_id uuid, p_status character varying, p_role character varying, p_is_owner boolean, p_accepted_terms_version uuid) TO anon;
GRANT ALL ON FUNCTION public.create_user_account(p_first_name character varying, p_last_name character varying, p_email character varying, p_password_hash character varying, p_cpf character varying, p_phone character varying, p_birth_date date, p_company_id uuid, p_status character varying, p_role character varying, p_is_owner boolean, p_accepted_terms_version uuid) TO authenticated;
GRANT ALL ON FUNCTION public.create_user_account(p_first_name character varying, p_last_name character varying, p_email character varying, p_password_hash character varying, p_cpf character varying, p_phone character varying, p_birth_date date, p_company_id uuid, p_status character varying, p_role character varying, p_is_owner boolean, p_accepted_terms_version uuid) TO service_role;


--
-- Name: FUNCTION credit_company_balance(p_company_id uuid, p_amount numeric, p_stripe_payment_id text, p_type text, p_description text); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.credit_company_balance(p_company_id uuid, p_amount numeric, p_stripe_payment_id text, p_type text, p_description text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.credit_company_balance(p_company_id uuid, p_amount numeric, p_stripe_payment_id text, p_type text, p_description text) TO service_role;


--
-- Name: FUNCTION debit_company_balance(p_company_id uuid, p_amount numeric); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.debit_company_balance(p_company_id uuid, p_amount numeric) FROM PUBLIC;
GRANT ALL ON FUNCTION public.debit_company_balance(p_company_id uuid, p_amount numeric) TO service_role;


--
-- Name: FUNCTION get_agent_ucp_capabilities(p_agent_id uuid); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.get_agent_ucp_capabilities(p_agent_id uuid) TO anon;
GRANT ALL ON FUNCTION public.get_agent_ucp_capabilities(p_agent_id uuid) TO authenticated;
GRANT ALL ON FUNCTION public.get_agent_ucp_capabilities(p_agent_id uuid) TO service_role;


--
-- Name: FUNCTION get_token_usage_by_company(start_date timestamp with time zone, end_date timestamp with time zone); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.get_token_usage_by_company(start_date timestamp with time zone, end_date timestamp with time zone) TO anon;
GRANT ALL ON FUNCTION public.get_token_usage_by_company(start_date timestamp with time zone, end_date timestamp with time zone) TO authenticated;
GRANT ALL ON FUNCTION public.get_token_usage_by_company(start_date timestamp with time zone, end_date timestamp with time zone) TO service_role;


--
-- Name: FUNCTION get_token_usage_report(start_date timestamp with time zone, end_date timestamp with time zone); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.get_token_usage_report(start_date timestamp with time zone, end_date timestamp with time zone) TO anon;
GRANT ALL ON FUNCTION public.get_token_usage_report(start_date timestamp with time zone, end_date timestamp with time zone) TO authenticated;
GRANT ALL ON FUNCTION public.get_token_usage_report(start_date timestamp with time zone, end_date timestamp with time zone) TO service_role;


--
-- Name: FUNCTION get_user_for_login(p_email character varying); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.get_user_for_login(p_email character varying) TO anon;
GRANT ALL ON FUNCTION public.get_user_for_login(p_email character varying) TO authenticated;
GRANT ALL ON FUNCTION public.get_user_for_login(p_email character varying) TO service_role;


--
-- Name: FUNCTION get_widget_agent_public(p_agent_id uuid); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.get_widget_agent_public(p_agent_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.get_widget_agent_public(p_agent_id uuid) TO anon;
GRANT ALL ON FUNCTION public.get_widget_agent_public(p_agent_id uuid) TO authenticated;
GRANT ALL ON FUNCTION public.get_widget_agent_public(p_agent_id uuid) TO service_role;


--
-- Name: FUNCTION get_widget_messages_scoped(p_session_id text, p_company_id uuid, p_agent_id uuid, p_origin text, p_exp bigint, p_proof text); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.get_widget_messages_scoped(p_session_id text, p_company_id uuid, p_agent_id uuid, p_origin text, p_exp bigint, p_proof text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.get_widget_messages_scoped(p_session_id text, p_company_id uuid, p_agent_id uuid, p_origin text, p_exp bigint, p_proof text) TO anon;
GRANT ALL ON FUNCTION public.get_widget_messages_scoped(p_session_id text, p_company_id uuid, p_agent_id uuid, p_origin text, p_exp bigint, p_proof text) TO authenticated;
GRANT ALL ON FUNCTION public.get_widget_messages_scoped(p_session_id text, p_company_id uuid, p_agent_id uuid, p_origin text, p_exp bigint, p_proof text) TO service_role;


--
-- Name: FUNCTION increment_conversation_unread(p_conversation_id uuid, p_company_id uuid, p_preview text, p_last_message_at timestamp with time zone); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.increment_conversation_unread(p_conversation_id uuid, p_company_id uuid, p_preview text, p_last_message_at timestamp with time zone) TO anon;
GRANT ALL ON FUNCTION public.increment_conversation_unread(p_conversation_id uuid, p_company_id uuid, p_preview text, p_last_message_at timestamp with time zone) TO authenticated;
GRANT ALL ON FUNCTION public.increment_conversation_unread(p_conversation_id uuid, p_company_id uuid, p_preview text, p_last_message_at timestamp with time zone) TO service_role;


--
-- Name: FUNCTION process_token_usage_outbox(p_limit integer, p_stale_minutes integer); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.process_token_usage_outbox(p_limit integer, p_stale_minutes integer) FROM PUBLIC;
GRANT ALL ON FUNCTION public.process_token_usage_outbox(p_limit integer, p_stale_minutes integer) TO service_role;


--
-- Name: FUNCTION reset_company_balance(p_company_id uuid, p_amount numeric, p_stripe_payment_id text, p_description text); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.reset_company_balance(p_company_id uuid, p_amount numeric, p_stripe_payment_id text, p_description text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.reset_company_balance(p_company_id uuid, p_amount numeric, p_stripe_payment_id text, p_description text) TO service_role;


--
-- Name: FUNCTION rpc_attendance_transition(p_action text, p_company_id uuid, p_conversation_id uuid, p_session_id text, p_agent_id uuid, p_actor_type text, p_actor_user_id uuid, p_actor_agent_id uuid, p_payload jsonb, p_first_response_deadline timestamp with time zone, p_resolution_deadline timestamp with time zone, p_sla_level text, p_policy_snapshot jsonb, p_started_at timestamp with time zone); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.rpc_attendance_transition(p_action text, p_company_id uuid, p_conversation_id uuid, p_session_id text, p_agent_id uuid, p_actor_type text, p_actor_user_id uuid, p_actor_agent_id uuid, p_payload jsonb, p_first_response_deadline timestamp with time zone, p_resolution_deadline timestamp with time zone, p_sla_level text, p_policy_snapshot jsonb, p_started_at timestamp with time zone) TO authenticated;
GRANT ALL ON FUNCTION public.rpc_attendance_transition(p_action text, p_company_id uuid, p_conversation_id uuid, p_session_id text, p_agent_id uuid, p_actor_type text, p_actor_user_id uuid, p_actor_agent_id uuid, p_payload jsonb, p_first_response_deadline timestamp with time zone, p_resolution_deadline timestamp with time zone, p_sla_level text, p_policy_snapshot jsonb, p_started_at timestamp with time zone) TO service_role;


--
-- Name: FUNCTION rpc_list_contacts(p_company_id uuid, p_search text, p_channel text, p_created_from timestamp with time zone, p_created_to timestamp with time zone, p_limit integer, p_offset integer); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.rpc_list_contacts(p_company_id uuid, p_search text, p_channel text, p_created_from timestamp with time zone, p_created_to timestamp with time zone, p_limit integer, p_offset integer) FROM PUBLIC;
GRANT ALL ON FUNCTION public.rpc_list_contacts(p_company_id uuid, p_search text, p_channel text, p_created_from timestamp with time zone, p_created_to timestamp with time zone, p_limit integer, p_offset integer) TO service_role;


--
-- Name: FUNCTION rpc_metrics_attendance(p_company_id uuid, p_start timestamp with time zone, p_end timestamp with time zone); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.rpc_metrics_attendance(p_company_id uuid, p_start timestamp with time zone, p_end timestamp with time zone) FROM PUBLIC;
GRANT ALL ON FUNCTION public.rpc_metrics_attendance(p_company_id uuid, p_start timestamp with time zone, p_end timestamp with time zone) TO service_role;


--
-- Name: FUNCTION rpc_metrics_by_agent(p_company_id uuid, p_start timestamp with time zone, p_end timestamp with time zone); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.rpc_metrics_by_agent(p_company_id uuid, p_start timestamp with time zone, p_end timestamp with time zone) FROM PUBLIC;
GRANT ALL ON FUNCTION public.rpc_metrics_by_agent(p_company_id uuid, p_start timestamp with time zone, p_end timestamp with time zone) TO service_role;


--
-- Name: FUNCTION rpc_metrics_summary(p_company_id uuid, p_start timestamp with time zone, p_end timestamp with time zone); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.rpc_metrics_summary(p_company_id uuid, p_start timestamp with time zone, p_end timestamp with time zone) FROM PUBLIC;
GRANT ALL ON FUNCTION public.rpc_metrics_summary(p_company_id uuid, p_start timestamp with time zone, p_end timestamp with time zone) TO service_role;


--
-- Name: FUNCTION rpc_metrics_timeseries(p_company_id uuid, p_start timestamp with time zone, p_end timestamp with time zone); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.rpc_metrics_timeseries(p_company_id uuid, p_start timestamp with time zone, p_end timestamp with time zone) FROM PUBLIC;
GRANT ALL ON FUNCTION public.rpc_metrics_timeseries(p_company_id uuid, p_start timestamp with time zone, p_end timestamp with time zone) TO service_role;


--
-- Name: FUNCTION security_audit_admin_users_role(); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.security_audit_admin_users_role() TO anon;
GRANT ALL ON FUNCTION public.security_audit_admin_users_role() TO authenticated;
GRANT ALL ON FUNCTION public.security_audit_admin_users_role() TO service_role;


--
-- Name: FUNCTION security_audit_agent_http_tools_url(); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.security_audit_agent_http_tools_url() TO anon;
GRANT ALL ON FUNCTION public.security_audit_agent_http_tools_url() TO authenticated;
GRANT ALL ON FUNCTION public.security_audit_agent_http_tools_url() TO service_role;


--
-- Name: FUNCTION security_audit_companies_webhook_url(); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.security_audit_companies_webhook_url() TO anon;
GRANT ALL ON FUNCTION public.security_audit_companies_webhook_url() TO authenticated;
GRANT ALL ON FUNCTION public.security_audit_companies_webhook_url() TO service_role;


--
-- Name: FUNCTION security_audit_resource_delete(); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.security_audit_resource_delete() TO anon;
GRANT ALL ON FUNCTION public.security_audit_resource_delete() TO authenticated;
GRANT ALL ON FUNCTION public.security_audit_resource_delete() TO service_role;


--
-- Name: FUNCTION security_audit_users_v2_status(); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.security_audit_users_v2_status() TO anon;
GRANT ALL ON FUNCTION public.security_audit_users_v2_status() TO authenticated;
GRANT ALL ON FUNCTION public.security_audit_users_v2_status() TO service_role;


--
-- Name: FUNCTION ucp_connections_touch_config_updated_at(); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.ucp_connections_touch_config_updated_at() TO anon;
GRANT ALL ON FUNCTION public.ucp_connections_touch_config_updated_at() TO authenticated;
GRANT ALL ON FUNCTION public.ucp_connections_touch_config_updated_at() TO service_role;


--
-- Name: FUNCTION update_agent_delegations_updated_at(); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.update_agent_delegations_updated_at() TO anon;
GRANT ALL ON FUNCTION public.update_agent_delegations_updated_at() TO authenticated;
GRANT ALL ON FUNCTION public.update_agent_delegations_updated_at() TO service_role;


--
-- Name: FUNCTION update_documents_updated_at(); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.update_documents_updated_at() TO anon;
GRANT ALL ON FUNCTION public.update_documents_updated_at() TO authenticated;
GRANT ALL ON FUNCTION public.update_documents_updated_at() TO service_role;


--
-- Name: FUNCTION update_ucp_updated_at(); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.update_ucp_updated_at() TO anon;
GRANT ALL ON FUNCTION public.update_ucp_updated_at() TO authenticated;
GRANT ALL ON FUNCTION public.update_ucp_updated_at() TO service_role;


--
-- Name: FUNCTION update_updated_at_column(); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.update_updated_at_column() TO anon;
GRANT ALL ON FUNCTION public.update_updated_at_column() TO authenticated;
GRANT ALL ON FUNCTION public.update_updated_at_column() TO service_role;


--
-- Name: FUNCTION write_security_audit_log(p_action text, p_company_id uuid, p_resource_type text, p_resource_id uuid, p_status text, p_details jsonb); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.write_security_audit_log(p_action text, p_company_id uuid, p_resource_type text, p_resource_id uuid, p_status text, p_details jsonb) FROM PUBLIC;
GRANT ALL ON FUNCTION public.write_security_audit_log(p_action text, p_company_id uuid, p_resource_type text, p_resource_id uuid, p_status text, p_details jsonb) TO service_role;


--
-- Name: TABLE admin_users; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.admin_users TO service_role;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.admin_users TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.admin_users TO anon;


--
-- Name: TABLE agent_attendance_settings; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_attendance_settings TO authenticated;
GRANT ALL ON TABLE public.agent_attendance_settings TO service_role;


--
-- Name: TABLE agent_delegations; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_delegations TO authenticated;
GRANT ALL ON TABLE public.agent_delegations TO service_role;


--
-- Name: TABLE agent_http_tools; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_http_tools TO service_role;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.agent_http_tools TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.agent_http_tools TO anon;


--
-- Name: TABLE agent_mcp_connections; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_mcp_connections TO service_role;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.agent_mcp_connections TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.agent_mcp_connections TO anon;


--
-- Name: TABLE agent_mcp_tools; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agent_mcp_tools TO service_role;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.agent_mcp_tools TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.agent_mcp_tools TO anon;


--
-- Name: TABLE agents; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.agents TO service_role;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.agents TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.agents TO anon;


--
-- Name: TABLE attendance_sessions; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.attendance_sessions TO authenticated;
GRANT ALL ON TABLE public.attendance_sessions TO service_role;


--
-- Name: TABLE attendance_sla; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.attendance_sla TO authenticated;
GRANT ALL ON TABLE public.attendance_sla TO service_role;


--
-- Name: TABLE checkpoint_blobs; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.checkpoint_blobs TO anon;
GRANT ALL ON TABLE public.checkpoint_blobs TO authenticated;
GRANT ALL ON TABLE public.checkpoint_blobs TO service_role;


--
-- Name: TABLE checkpoint_migrations; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.checkpoint_migrations TO anon;
GRANT ALL ON TABLE public.checkpoint_migrations TO authenticated;
GRANT ALL ON TABLE public.checkpoint_migrations TO service_role;


--
-- Name: TABLE checkpoint_writes; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.checkpoint_writes TO anon;
GRANT ALL ON TABLE public.checkpoint_writes TO authenticated;
GRANT ALL ON TABLE public.checkpoint_writes TO service_role;


--
-- Name: TABLE checkpoints; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.checkpoints TO service_role;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.checkpoints TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.checkpoints TO anon;


--
-- Name: TABLE companies; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.companies TO service_role;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.companies TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.companies TO anon;


--
-- Name: TABLE company_attendance_settings; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.company_attendance_settings TO authenticated;
GRANT ALL ON TABLE public.company_attendance_settings TO service_role;


--
-- Name: TABLE company_credits; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.company_credits TO anon;
GRANT ALL ON TABLE public.company_credits TO authenticated;
GRANT ALL ON TABLE public.company_credits TO service_role;


--
-- Name: TABLE conversation_events; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.conversation_events TO authenticated;
GRANT ALL ON TABLE public.conversation_events TO service_role;


--
-- Name: TABLE conversation_inactivity_timers; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.conversation_inactivity_timers TO authenticated;
GRANT ALL ON TABLE public.conversation_inactivity_timers TO service_role;


--
-- Name: TABLE conversation_logs; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.conversation_logs TO service_role;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.conversation_logs TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.conversation_logs TO anon;


--
-- Name: TABLE conversations; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.conversations TO authenticated;
GRANT ALL ON TABLE public.conversations TO service_role;


--
-- Name: TABLE credit_transactions; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.credit_transactions TO anon;
GRANT ALL ON TABLE public.credit_transactions TO authenticated;
GRANT ALL ON TABLE public.credit_transactions TO service_role;


--
-- Name: TABLE documents; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.documents TO service_role;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.documents TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.documents TO anon;


--
-- Name: TABLE handoff_notification_recipients; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.handoff_notification_recipients TO authenticated;
GRANT ALL ON TABLE public.handoff_notification_recipients TO service_role;


--
-- Name: TABLE integrations; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.integrations TO anon;
GRANT ALL ON TABLE public.integrations TO authenticated;
GRANT ALL ON TABLE public.integrations TO service_role;


--
-- Name: TABLE internal_whatsapp_blocklist; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.internal_whatsapp_blocklist TO authenticated;
GRANT ALL ON TABLE public.internal_whatsapp_blocklist TO service_role;


--
-- Name: TABLE invites; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.invites TO anon;
GRANT ALL ON TABLE public.invites TO authenticated;
GRANT ALL ON TABLE public.invites TO service_role;


--
-- Name: TABLE leads; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.leads TO anon;
GRANT ALL ON TABLE public.leads TO authenticated;
GRANT ALL ON TABLE public.leads TO service_role;


--
-- Name: TABLE legal_documents; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.legal_documents TO anon;
GRANT ALL ON TABLE public.legal_documents TO authenticated;
GRANT ALL ON TABLE public.legal_documents TO service_role;


--
-- Name: TABLE llm_pricing; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.llm_pricing TO anon;
GRANT ALL ON TABLE public.llm_pricing TO authenticated;
GRANT ALL ON TABLE public.llm_pricing TO service_role;


--
-- Name: TABLE mcp_oauth_clients; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.mcp_oauth_clients TO service_role;


--
-- Name: TABLE mcp_servers; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.mcp_servers TO anon;
GRANT ALL ON TABLE public.mcp_servers TO authenticated;
GRANT ALL ON TABLE public.mcp_servers TO service_role;


--
-- Name: TABLE memory_processing_locks; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.memory_processing_locks TO service_role;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.memory_processing_locks TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.memory_processing_locks TO anon;


--
-- Name: TABLE memory_settings; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.memory_settings TO anon;
GRANT ALL ON TABLE public.memory_settings TO authenticated;
GRANT ALL ON TABLE public.memory_settings TO service_role;


--
-- Name: TABLE messages; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.messages TO authenticated;
GRANT ALL ON TABLE public.messages TO service_role;


--
-- Name: TABLE notification_deliveries; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.notification_deliveries TO authenticated;
GRANT ALL ON TABLE public.notification_deliveries TO service_role;


--
-- Name: TABLE password_reset_tokens; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.password_reset_tokens TO anon;
GRANT ALL ON TABLE public.password_reset_tokens TO authenticated;
GRANT ALL ON TABLE public.password_reset_tokens TO service_role;


--
-- Name: TABLE payment_history; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.payment_history TO anon;
GRANT ALL ON TABLE public.payment_history TO authenticated;
GRANT ALL ON TABLE public.payment_history TO service_role;


--
-- Name: TABLE plans; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.plans TO anon;
GRANT ALL ON TABLE public.plans TO authenticated;
GRANT ALL ON TABLE public.plans TO service_role;


--
-- Name: TABLE platform_provider_alerts; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.platform_provider_alerts TO service_role;


--
-- Name: TABLE platform_settings; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.platform_settings TO service_role;


--
-- Name: TABLE sanitization_jobs; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.sanitization_jobs TO authenticated;
GRANT ALL ON TABLE public.sanitization_jobs TO service_role;


--
-- Name: TABLE session_summaries; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.session_summaries TO anon;
GRANT ALL ON TABLE public.session_summaries TO authenticated;
GRANT ALL ON TABLE public.session_summaries TO service_role;


--
-- Name: TABLE sla_events; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.sla_events TO authenticated;
GRANT ALL ON TABLE public.sla_events TO service_role;


--
-- Name: TABLE sla_policies; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.sla_policies TO authenticated;
GRANT ALL ON TABLE public.sla_policies TO service_role;


--
-- Name: TABLE subscriptions; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.subscriptions TO anon;
GRANT ALL ON TABLE public.subscriptions TO authenticated;
GRANT ALL ON TABLE public.subscriptions TO service_role;


--
-- Name: TABLE system_logs; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.system_logs TO service_role;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_logs TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.system_logs TO anon;


--
-- Name: TABLE token_usage_logs; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.token_usage_logs TO anon;
GRANT ALL ON TABLE public.token_usage_logs TO authenticated;
GRANT ALL ON TABLE public.token_usage_logs TO service_role;


--
-- Name: TABLE token_usage_outbox; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.token_usage_outbox TO service_role;


--
-- Name: TABLE ucp_connections; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.ucp_connections TO anon;
GRANT ALL ON TABLE public.ucp_connections TO authenticated;
GRANT ALL ON TABLE public.ucp_connections TO service_role;


--
-- Name: TABLE ucp_connection_summary; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.ucp_connection_summary TO anon;
GRANT ALL ON TABLE public.ucp_connection_summary TO authenticated;
GRANT ALL ON TABLE public.ucp_connection_summary TO service_role;


--
-- Name: TABLE user_memories; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.user_memories TO anon;
GRANT ALL ON TABLE public.user_memories TO authenticated;
GRANT ALL ON TABLE public.user_memories TO service_role;


--
-- Name: TABLE users_v2; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.users_v2 TO service_role;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.users_v2 TO authenticated;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.users_v2 TO anon;


--
-- Name: TABLE widget_rate_limits; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.widget_rate_limits TO anon;
GRANT ALL ON TABLE public.widget_rate_limits TO authenticated;
GRANT ALL ON TABLE public.widget_rate_limits TO service_role;


--
-- Name: DEFAULT PRIVILEGES FOR SEQUENCES; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO postgres;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO service_role;


--
-- Name: DEFAULT PRIVILEGES FOR SEQUENCES; Type: DEFAULT ACL; Schema: public; Owner: -
--



--
-- Name: DEFAULT PRIVILEGES FOR FUNCTIONS; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS TO postgres;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS TO service_role;


--
-- Name: DEFAULT PRIVILEGES FOR FUNCTIONS; Type: DEFAULT ACL; Schema: public; Owner: -
--



--
-- Name: DEFAULT PRIVILEGES FOR TABLES; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES TO postgres;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES TO service_role;


--
-- Name: DEFAULT PRIVILEGES FOR TABLES; Type: DEFAULT ACL; Schema: public; Owner: -
--



--
-- PostgreSQL database dump complete
--


