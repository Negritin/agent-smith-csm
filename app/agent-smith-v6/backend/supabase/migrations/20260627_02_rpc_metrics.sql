-- ============================================================================
-- CMA Sprint S4 — F2 Métricas: RPCs de summary + timeseries.
--
-- Espelha o template de segurança das RPCs de billing (20260626_03_billing_rpcs.sql):
-- SECURITY DEFINER, SET search_path=public, REVOKE PUBLIC/anon/authenticated,
-- GRANT EXECUTE só a service_role. Chamadas pela rota Next via getSupabaseAdmin()
-- (service_role) → tenant scoping fica DENTRO do WHERE/JOIN (p_company_id).
--
-- Total de Mensagens / série de mensagens: JOIN messages→conversations por
-- conversation_id (messages.company_id é backfill nullable → subcontaria).
-- Buckets de dia em America/Sao_Paulo p/ casar com os bounds -03:00 do HTTP.
--
-- ⚠️ FILES ONLY: NÃO aplicar a nenhum banco vivo nesta sprint. Listado para o
--    usuário aplicar manualmente. Requer o índice 20260627_03 p/ performance.
-- ============================================================================

BEGIN;

DROP FUNCTION IF EXISTS public.rpc_metrics_summary(uuid, timestamptz, timestamptz);
DROP FUNCTION IF EXISTS public.rpc_metrics_timeseries(uuid, timestamptz, timestamptz);

-- ---------------------------------------------------------------------------
-- rpc_metrics_summary: os 6 cards (SPEC §2.2 / D3 / D4).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.rpc_metrics_summary(
    p_company_id uuid,
    p_start      timestamptz,
    p_end        timestamptz
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = public
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

    -- Leads Gerados = COUNT(DISTINCT contact_key) no range (D3, mesmo key dos Contatos).
    SELECT count(DISTINCT COALESCE(c.user_id::text, NULLIF(c.user_phone, ''), c.session_id))
      INTO v_leads
      FROM public.conversations c
     WHERE c.company_id = p_company_id
       AND c.created_at >= p_start
       AND c.created_at <  p_end;

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

REVOKE ALL ON FUNCTION public.rpc_metrics_summary(uuid, timestamptz, timestamptz)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rpc_metrics_summary(uuid, timestamptz, timestamptz)
    TO service_role;

-- ---------------------------------------------------------------------------
-- rpc_metrics_timeseries: conversas + mensagens por dia, gaps preenchidos com 0.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.rpc_metrics_timeseries(
    p_company_id uuid,
    p_start      timestamptz,
    p_end        timestamptz
) RETURNS TABLE(date date, conversations bigint, messages bigint)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = public
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

REVOKE ALL ON FUNCTION public.rpc_metrics_timeseries(uuid, timestamptz, timestamptz)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rpc_metrics_timeseries(uuid, timestamptz, timestamptz)
    TO service_role;

COMMIT;
