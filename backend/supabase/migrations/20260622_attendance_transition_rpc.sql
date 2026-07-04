-- S2 — Motor central de transição de status (D1 / §8.1 / §23).
--
-- UMA RPC transacional única (public.rpc_attendance_transition) é a ÚNICA forma
-- de escrever public.conversations.status. Valida a máquina de estados (§6.3),
-- tenancy (§7.10 regra 5) e timestamps, e grava conversations + attendance_sessions
-- + conversation_events (+ attendance_sla quando há SLA) na MESMA transação.
--
-- Idempotência (§7.3): eventos one-shot recebem idempotency_key
-- ('{attendance_session_id}:{event_type}') e usam INSERT ... ON CONFLICT DO NOTHING
-- contra uq_conversation_events_idempotency. Eventos repetíveis (ai/customer/human
-- message, note) gravam idempotency_key NULL.
--   NOTA: as chaves de conversation_events ('{session}:{event_type}') são DISTINTAS
--   das chaves de notification_deliveries ('{session}:{event_type}:{recipient_id}',
--   definidas em S4/§11.1). Não confundir as convenções.
--
-- Concorrência (§7.2/§24): a criação de sessão usa INSERT ... ON CONFLICT DO NOTHING
-- contra uq_attendance_sessions_one_open_per_conversation + re-leitura. Colisão do
-- buffer WhatsApp é tratada como "sessão já existe" (sucesso), nunca 500.
--
-- Contrato de SLA (autoritativo, definido AQUI — §22 item 5): request_handoff aceita
-- p_first_response_deadline / p_resolution_deadline (timestamptz), p_sla_level (text)
-- e p_policy_snapshot (jsonb), todos DEFAULT NULL, pré-calculados pelo SlaService (S3,
-- read-only). Regra de NULL: os 4 NULL ⇒ NENHUMA linha attendance_sla é criada
-- (caminho "none"); preenchidos ⇒ grava attendance_sla no MESMO commit do handoff.
--
-- Aditivo / idempotente: CREATE OR REPLACE FUNCTION; nenhum write site legado é
-- convertido aqui (S5/S6/S7). DDL idempotente.
--
-- Rollback (documentado):
--   drop function public.rpc_attendance_transition(text, uuid, uuid, text, uuid,
--     text, uuid, uuid, jsonb, timestamptz, timestamptz, text, jsonb, timestamptz);
--   drop function public._attendance_enqueue_handoff_notifications(uuid, uuid, uuid, uuid);
--   ALTER TABLE public.conversation_events DROP CONSTRAINT conversation_events_event_type_check;
--   (restaurar o CHECK original sem 'reopened_by_admin').
--
-- OUTBOX MESMO-COMMIT (S4/§8.3): request_handoff insere notification_deliveries
-- (status='pending') na MESMA transação do handoff, via
-- _attendance_enqueue_handoff_notifications. claim/manual NÃO notifica (§11.1).
--
-- SPEC §6.1, §6.2, §6.3, §6.4, §7.1, §7.2, §7.3, §7.5, §8.1, §10.1, §23 D1, §24.

-- ----------------------------------------------------------------------------
-- Ajuste aditivo do CHECK de conversation_events.event_type para incluir o evento
-- 'reopened_by_admin' (S2 entregável #7). O CHECK original (S1, §7.3) não o lista;
-- a reabertura por admin é introduzida neste sprint. Recriação idempotente do CHECK.
-- ----------------------------------------------------------------------------
-- ----------------------------------------------------------------------------
-- Idempotência da assinatura da RPC: a versão anterior NÃO tinha p_started_at.
-- Adicionar um parâmetro (mesmo com DEFAULT) cria uma NOVA função em vez de
-- substituir a antiga, deixando duas sobrecargas e tornando a chamada por
-- argumentos nomeados ambígua. Removemos explicitamente a assinatura antiga (sem
-- p_started_at) antes do CREATE OR REPLACE abaixo. IF EXISTS torna idempotente.
-- ----------------------------------------------------------------------------
DROP FUNCTION IF EXISTS public.rpc_attendance_transition(
    text, uuid, uuid, text, uuid, text, uuid, uuid, jsonb,
    timestamptz, timestamptz, text, jsonb
);

DO $$
BEGIN
  ALTER TABLE public.conversation_events
    DROP CONSTRAINT IF EXISTS conversation_events_event_type_check;

  ALTER TABLE public.conversation_events
    ADD CONSTRAINT conversation_events_event_type_check
    CHECK (event_type IN (
      'attendance_started',
      'ai_message_sent',
      'customer_message_received',
      'handoff_requested',
      'handoff_notified',
      'human_claimed',
      'human_message_sent',
      'returned_to_ai',
      'resolved_by_human',
      'resolved_by_agent',
      'closed_by_human',
      'closed_by_agent',
      'closed_by_system',
      'auto_close_scheduled',
      'auto_close_cancelled',
      'timeout_closed',
      'reopened_by_customer',
      'reopened_by_admin',
      'note_added'
    ));
END
$$;

-- ----------------------------------------------------------------------------
-- public.rpc_attendance_transition
--
-- Identificação da conversa: por p_conversation_id OU
-- (p_session_id + p_company_id + p_agent_id) — caminho da handoff tool (§10.1).
--
-- p_action ∈ request_handoff | claim | return_to_ai | resolve | close | reopen
--           | record_human_message | record_ai_message | record_customer_message
--           | create_event | add_note
--
-- p_actor_type ∈ customer | agent | human | system
--
-- Retorno (jsonb): { status, conversation_id, attendance_session_id,
--                    attendance_sla_id, event_id, previous_status }
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.rpc_attendance_transition(
    p_action text,
    p_company_id uuid,
    p_conversation_id uuid DEFAULT NULL,
    p_session_id text DEFAULT NULL,
    p_agent_id uuid DEFAULT NULL,
    p_actor_type text DEFAULT NULL,
    p_actor_user_id uuid DEFAULT NULL,
    p_actor_agent_id uuid DEFAULT NULL,
    p_payload jsonb DEFAULT '{}'::jsonb,
    -- Contrato de SLA (somente request_handoff/claim); NULL ⇒ não cria attendance_sla.
    p_first_response_deadline timestamptz DEFAULT NULL,
    p_resolution_deadline timestamptz DEFAULT NULL,
    p_sla_level text DEFAULT NULL,
    p_policy_snapshot jsonb DEFAULT NULL,
    -- Âncora ÚNICA do SLA (§7.4/§7.5): o SlaService calcula os deadlines a partir
    -- DESTE started_at e o passa aqui para que attendance_sla.started_at use o MESMO
    -- instante. DEFAULT now() (parâmetro NOMEADO) preserva callers que não o enviam.
    p_started_at timestamptz DEFAULT now()
) RETURNS jsonb
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

-- ----------------------------------------------------------------------------
-- Helper: criação/recuperação idempotente e concorrência-safe da sessão aberta.
-- INSERT ... ON CONFLICT DO NOTHING contra uq_attendance_sessions_one_open_per_conversation
-- + re-leitura. Colisão = "sessão já existe" (§7.2/§24). Retorna o id da sessão viva.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public._attendance_ensure_open_session(
    p_conversation_id uuid,
    p_company_id uuid,
    p_agent_id uuid,
    p_status text,
    p_payload jsonb,
    p_actor_type text,
    p_actor_agent_id uuid,
    p_actor_user_id uuid
) RETURNS uuid
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

-- ----------------------------------------------------------------------------
-- Helper: gravação de conversation_events com idempotência (§7.3).
-- p_idempotency_key NULL ⇒ evento repetível; preenchida ⇒ one-shot retry-safe.
-- Retorna o id do evento (existente, em caso de colisão idempotente).
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public._attendance_record_event(
    p_conversation_id uuid,
    p_attendance_session_id uuid,
    p_company_id uuid,
    p_agent_id uuid,
    p_event_type text,
    p_actor_type text,
    p_actor_user_id uuid,
    p_actor_agent_id uuid,
    p_metadata jsonb,
    p_idempotency_key text
) RETURNS uuid
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

-- ----------------------------------------------------------------------------
-- Helper: criação do snapshot attendance_sla no MESMO commit do handoff (§7.5).
-- Idempotente por attendance_session_id (UNIQUE). policy_snapshot é congelado.
-- Também grava sla_event 'sla_started' (idempotente — não está no índice parcial).
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public._attendance_create_sla(
    p_attendance_session_id uuid,
    p_conversation_id uuid,
    p_company_id uuid,
    p_sla_level text,
    p_started_at timestamptz,
    p_first_response_deadline timestamptz,
    p_resolution_deadline timestamptz,
    p_policy_snapshot jsonb
) RETURNS uuid
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

-- ----------------------------------------------------------------------------
-- Helper: OUTBOX transacional de notificações de handoff (S4/§8.3/§11.1).
--
-- Cria notification_deliveries(status='pending') na MESMA transação do handoff
-- (atomicidade: ou o handoff e as deliveries existem juntos, ou nada). Chamado
-- SÓ pela action request_handoff (handoff sem dono humano); claim/manual NÃO
-- chama (não notifica, §11.1).
--
-- Seleção (§11.1): handoff_notification_recipients com company_id da conversa,
-- enabled=true e (agent_id = agente da conversa OU agent_id IS NULL) — união dos
-- específicos do agente com os da empresa.
--
-- DEDUP por recipient_normalized (§11.1): o mesmo número/email listado nos dois
-- escopos é enfileirado UMA vez, preferindo a LINHA ESPECÍFICA DO AGENTE
-- (display_name/recipient_id do agente). A preferência é resolvida por
-- DISTINCT ON (channel, recipient_normalized) ORDER BY (agent_id IS NULL) — false
-- (linha do agente) ordena antes de true (linha da empresa).
--
-- Idempotência (§11.1): idempotency_key = '{attendance_session_id}:{event_type}:{recipient_id}'
-- (event_type fixo 'handoff_requested') contra uq_notification_delivery_idempotency,
-- via INSERT ... ON CONFLICT DO NOTHING. Retry da RPC não duplica linhas.
--
-- Os ENVIOS (WhatsApp provider-aware / email) e o backoff ficam no
-- NotificationService.process_pending (S4); aqui só o enfileiramento atômico.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public._attendance_enqueue_handoff_notifications(
    p_conversation_id uuid,
    p_attendance_session_id uuid,
    p_company_id uuid,
    p_agent_id uuid
) RETURNS void
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

-- ----------------------------------------------------------------------------
-- Grants: a RPC e helpers rodam sob service_role (backend/BFF/workers, §7.10 regra 4).
-- anon não recebe execução.
-- ----------------------------------------------------------------------------
REVOKE ALL ON FUNCTION public.rpc_attendance_transition(
    text, uuid, uuid, text, uuid, text, uuid, uuid, jsonb,
    timestamptz, timestamptz, text, jsonb, timestamptz
) FROM anon;
GRANT EXECUTE ON FUNCTION public.rpc_attendance_transition(
    text, uuid, uuid, text, uuid, text, uuid, uuid, jsonb,
    timestamptz, timestamptz, text, jsonb, timestamptz
) TO service_role;

-- §7.10 regra 4: os helpers internos não devem ser executáveis por PUBLIC/anon
-- (default do Postgres concede EXECUTE a PUBLIC em funções novas). Espelha o
-- tratamento da RPC principal: só service_role executa. As tabelas têm RLS, mas
-- removemos a execução por PUBLIC para fechar o desvio do princípio "anon não executa".
REVOKE ALL ON FUNCTION public._attendance_ensure_open_session(
    uuid, uuid, uuid, text, jsonb, text, uuid, uuid
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public._attendance_ensure_open_session(
    uuid, uuid, uuid, text, jsonb, text, uuid, uuid
) TO service_role;

REVOKE ALL ON FUNCTION public._attendance_record_event(
    uuid, uuid, uuid, uuid, text, text, uuid, uuid, jsonb, text
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public._attendance_record_event(
    uuid, uuid, uuid, uuid, text, text, uuid, uuid, jsonb, text
) TO service_role;

REVOKE ALL ON FUNCTION public._attendance_create_sla(
    uuid, uuid, uuid, text, timestamptz, timestamptz, timestamptz, jsonb
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public._attendance_create_sla(
    uuid, uuid, uuid, text, timestamptz, timestamptz, timestamptz, jsonb
) TO service_role;

REVOKE ALL ON FUNCTION public._attendance_enqueue_handoff_notifications(
    uuid, uuid, uuid, uuid
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public._attendance_enqueue_handoff_notifications(
    uuid, uuid, uuid, uuid
) TO service_role;
