-- ============================================================================
-- Métricas — RPCs das abas "Atendimentos" (+ SLA) e "Agentes".
-- SPEC: SPEC_METRICAS_ATENDIMENTO_AGENTES.md §2 / §4.3 / §5.1 / §5.2 / §7.
--
-- Espelha o template de segurança das RPCs de métricas/billing
-- (20260627_02_rpc_metrics.sql / 20260626_03_billing_rpcs.sql):
-- SECURITY DEFINER, SET search_path=public, REVOKE PUBLIC/anon/authenticated,
-- GRANT EXECUTE só a service_role. Chamadas pela rota Next via getSupabaseAdmin()
-- (service_role) → tenant scoping fica DENTRO do WHERE/JOIN (p_company_id).
--
-- REGRAS CRÍTICAS (§2 / §4.3):
--  - Atendimento assumido = attendance_sessions com human_taken_at no período,
--    agrupado por human_taken_by_user_id (a coluna user_id é SEMPRE NULL no banco
--    real → JAMAIS agrupar por ela).
--  - SLA furado: a fonte da verdade é sla_events.created_at (não existe coluna
--    "breached_at"/"missed_at" em attendance_sla). breached_count = count(*) da
--    MESMA query de sla_events da lista (sem LIMIT), p/ card bater com a lista.
--  - by-agent: INNER JOIN em agents (descarta agent_id NULL); agents.name é varchar
--    → RETURNS usa agents.name::text.
--
-- ⚠️ FILES ONLY: NÃO aplicar automaticamente a nenhum banco vivo. Listado para o
--    dono aplicar manualmente (padrão do repo, igual 20260627_01/02). Requer os
--    índices §7 abaixo p/ performance (já inclusos nesta migration).
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- Índices recomendados (SPEC §7). CREATE INDEX IF NOT EXISTS (NÃO CONCURRENTLY,
-- p/ rodar dentro da transação da migration).
-- ---------------------------------------------------------------------------

-- Ranking por admin (coorte por human_taken_at).
CREATE INDEX IF NOT EXISTS idx_attendance_sessions_company_taken
    ON public.attendance_sessions (company_id, human_taken_at);

-- Lista de furados (filtro exato da query de sla_events).
CREATE INDEX IF NOT EXISTS idx_sla_events_company_type_created
    ON public.sla_events (company_id, event_type, created_at);

-- % SLA (agregação por status).
CREATE INDEX IF NOT EXISTS idx_attendance_sla_company_resolution_status
    ON public.attendance_sla (company_id, resolution_status);
CREATE INDEX IF NOT EXISTS idx_attendance_sla_company_first_response_status
    ON public.attendance_sla (company_id, first_response_status);

-- Mensagens de IA — índice PARCIAL (§7): role tem cardinalidade 2; colocá-lo antes
-- de created_at estragaria o range por período. O parcial já restringe ao recorte de
-- IA (assistant + sender_user_id NULL) e ordena por data.
CREATE INDEX IF NOT EXISTS idx_messages_ai_created
    ON public.messages (created_at)
    WHERE role = 'assistant' AND sender_user_id IS NULL;

-- ---------------------------------------------------------------------------
-- rpc_metrics_attendance: aba "Atendimentos" (ranking por admin + bloco SLA).
-- Shape EXATO do §4.3:
--   { by_admin:[{user_id,name,role,is_owner,taken,resolved,open}],
--     sla:{first_response_pct, resolution_pct, breached_count,
--          breaches:[{conversation_id,customer,admin_name,kind,deadline,
--                     breached_at,delay_minutes}]} }
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS public.rpc_metrics_attendance(uuid, timestamptz, timestamptz);

CREATE OR REPLACE FUNCTION public.rpc_metrics_attendance(
    p_company_id uuid,
    p_start      timestamptz,
    p_end        timestamptz
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = public
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
    -- ----- by_admin: coorte = sessões com human_taken_at no período, agrupadas
    -- por human_taken_by_user_id (NÃO user_id). resolved/open saem da MESMA coorte.
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

    -- ----- sla percentuais: agrega attendance_sla (join sessões no período).
    -- first_response_pct = met/(met+missed); resolution_pct = met/(met+missed+breached).
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

    -- Divisão por zero → NULL (sem denominador = "—" no front).
    v_first_response_pct := CASE
        WHEN (v_fr_met + v_fr_missed) = 0 THEN NULL
        ELSE round(v_fr_met::numeric / (v_fr_met + v_fr_missed) * 100, 1)
    END;
    v_resolution_pct := CASE
        WHEN (v_rr_met + v_rr_missed + v_rr_breached) = 0 THEN NULL
        ELSE round(v_rr_met::numeric / (v_rr_met + v_rr_missed + v_rr_breached) * 100, 1)
    END;

    -- ----- sla.breached_count: count(*) da MESMA query de sla_events da lista (sem
    -- LIMIT). Card tem que bater com a lista → NÃO derivar de attendance_sla.status.
    SELECT count(*)
      INTO v_breached_count
      FROM public.sla_events e
     WHERE e.company_id = p_company_id
       AND e.event_type IN ('first_response_missed', 'resolution_missed', 'resolution_breached')
       AND e.created_at >= p_start
       AND e.created_at <  p_end;

    -- ----- sla.breaches: lista de furos (LIMIT 50, ordenada por atraso desc).
    -- breached_at = sla_events.created_at; delay_minutes = breached_at - deadline.
    -- deadline vem de attendance_sla (first_response_deadline ou resolution_deadline,
    -- conforme o kind derivado do event_type). admin_name via human_taken_by_user_id.
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

REVOKE ALL ON FUNCTION public.rpc_metrics_attendance(uuid, timestamptz, timestamptz)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rpc_metrics_attendance(uuid, timestamptz, timestamptz)
    TO service_role;

-- ---------------------------------------------------------------------------
-- rpc_metrics_by_agent: aba "Agentes" (SPEC §5.1 / §5.2).
-- messages = count(messages com role='assistant' AND sender_user_id IS NULL) JOIN
--   conversations por conversation_id, no período (messages.created_at), company_id,
--   GROUP BY conversations.agent_id.
-- conversations = count(DISTINCT conversation_id) no mesmo recorte.
-- INNER JOIN agents (descarta agent_id NULL); agent_name = agents.name::text.
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS public.rpc_metrics_by_agent(uuid, timestamptz, timestamptz);

CREATE OR REPLACE FUNCTION public.rpc_metrics_by_agent(
    p_company_id uuid,
    p_start      timestamptz,
    p_end        timestamptz
) RETURNS TABLE(
    agent_id      uuid,
    agent_name    text,
    messages      bigint,
    conversations bigint
)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = public
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

REVOKE ALL ON FUNCTION public.rpc_metrics_by_agent(uuid, timestamptz, timestamptz)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rpc_metrics_by_agent(uuid, timestamptz, timestamptz)
    TO service_role;

COMMIT;
