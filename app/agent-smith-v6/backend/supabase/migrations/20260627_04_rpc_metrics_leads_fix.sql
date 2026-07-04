-- ============================================================================
-- CMA — Fix: "Leads Gerados" deve contar apenas CONTATOS com EMAIL ou TELEFONE.
--
-- Problema: rpc_metrics_summary (20260627_02) calculava v_leads como
--   count(DISTINCT contact_key), contando TODO contact_key — inclusive os
--   keyed-por-session_id sem email nem telefone (linhas "—" da tela de Contatos,
--   ex.: conversas web órfãs de StressTest). Isso diverge da definição de "lead"
--   pedida pelo dono: lead = contato com e-mail OU telefone.
--
-- Correção: reescreve SOMENTE o bloco v_leads para espelhar rpc_list_contacts
-- (20260627_01) — mesmo contact_key + mesmos LEFT JOIN leads/users_v2 escopados
-- por company_id — e filtra HAVING (email não-vazio) OR (user_phone não-vazio).
-- Emails sintéticos do WhatsApp contam (têm telefone). Os demais 5 campos do
-- RETURN e o tipo (jsonb) ficam INTACTOS. Period-scoped por created_at (mantido).
--
-- ⚠️ FILES ONLY: NÃO aplicar a nenhum banco vivo automaticamente. Aplicar
--    manualmente (mesmo padrão de 20260627_02). Idempotente (CREATE OR REPLACE).
-- ============================================================================

BEGIN;

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

REVOKE ALL ON FUNCTION public.rpc_metrics_summary(uuid, timestamptz, timestamptz)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rpc_metrics_summary(uuid, timestamptz, timestamptz)
    TO service_role;

COMMIT;
