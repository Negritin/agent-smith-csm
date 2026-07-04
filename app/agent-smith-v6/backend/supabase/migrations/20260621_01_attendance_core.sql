-- ============================================================================
-- Sprint S1 — Atendimento/SLA/Handoff — attendance_core
-- SPEC-atendimento-sla-handoff-full.md §7.1 (campos em conversations),
-- §7.2 (attendance_sessions), §7.3 (conversation_events).
--
-- PRINCÍPIO: 100% aditivo e idempotente. Nenhuma coluna/constraint existente é
-- removida ou alterada. Nenhuma coluna nova é NOT NULL sem default em tabela
-- populada. NÃO toca em grants/policies de anon. NÃO troca a unicidade de
-- session_id (isso é o Sprint S5).
--
-- ORDEM OBRIGATÓRIA (§19 Fase 1 / S1 Contexto do Dev item 2):
--   1. Colunas novas em conversations (este arquivo).
--   2. attendance_sessions (este arquivo) — ANTES da FK que a referencia.
--   3. FK conversations_current_attendance_session_fkey DEFERRABLE INITIALLY
--      DEFERRED, criada DEPOIS de attendance_sessions existir (este arquivo).
--   4. conversation_events (este arquivo) — referencia attendance_sessions.
--
-- NÃO INCLUI (escopo de outros sprints):
--   - Índices CREATE INDEX CONCURRENTLY de conversations (20260621_90_concurrent_indexes.sql).
--   - conversations_status_check / conversations_sla_priority_check
--     (20260621_08_conversations_status_constraints.sql, NOT VALID + VALIDATE).
--   - DROP conversations_session_id_key / unicidade multi-tenant (S5).
--   - REVOKE/DROP POLICY de anon em conversations/messages (S11).
-- ============================================================================

-- ----------------------------------------------------------------------------
-- §7.1 — Campos rápidos e indexáveis em conversations (todos nullable ou com
-- DEFAULT, para não quebrar tabela populada).
-- ----------------------------------------------------------------------------
ALTER TABLE public.conversations
  ADD COLUMN IF NOT EXISTS assigned_user_id uuid REFERENCES public.users_v2(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS team_id uuid,
  ADD COLUMN IF NOT EXISTS current_attendance_session_id uuid,
  ADD COLUMN IF NOT EXISTS human_requested_at timestamptz,
  ADD COLUMN IF NOT EXISTS human_taken_at timestamptz,
  ADD COLUMN IF NOT EXISTS first_human_response_at timestamptz,
  ADD COLUMN IF NOT EXISTS returned_to_ai_at timestamptz,
  ADD COLUMN IF NOT EXISTS resolved_at timestamptz,
  ADD COLUMN IF NOT EXISTS closed_at timestamptz,
  ADD COLUMN IF NOT EXISTS closed_by_type text,
  ADD COLUMN IF NOT EXISTS closed_by_user_id uuid REFERENCES public.users_v2(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS closed_by_agent_id uuid REFERENCES public.agents(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS close_reason text,
  ADD COLUMN IF NOT EXISTS close_summary text,
  ADD COLUMN IF NOT EXISTS last_customer_message_at timestamptz,
  ADD COLUMN IF NOT EXISTS last_ai_message_at timestamptz,
  ADD COLUMN IF NOT EXISTS last_human_message_at timestamptz,
  ADD COLUMN IF NOT EXISTS customer_waiting_since timestamptz,
  ADD COLUMN IF NOT EXISTS agent_paused boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS agent_paused_reason text,
  ADD COLUMN IF NOT EXISTS sla_priority text;

-- NOTA: os CHECKs conversations_status_check e conversations_sla_priority_check
-- NÃO entram aqui. Eles exigem pré-flight de status legados e entram como
-- NOT VALID + VALIDATE em 20260621_08_conversations_status_constraints.sql.

-- ----------------------------------------------------------------------------
-- §7.2 — attendance_sessions (criada ANTES da FK em conversations).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.attendance_sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid NOT NULL REFERENCES public.conversations(id) ON DELETE CASCADE,
  company_id uuid NOT NULL REFERENCES public.companies(id) ON DELETE CASCADE,
  agent_id uuid REFERENCES public.agents(id) ON DELETE SET NULL,
  team_id uuid,
  user_id uuid,
  channel text NOT NULL DEFAULT 'web',
  status text NOT NULL DEFAULT 'open'
    CHECK (status IN (
      'open',
      'human_requested',
      'human_active',
      'pending_customer',
      'returned_to_ai',
      'resolved',
      'closed'
    )),
  started_at timestamptz NOT NULL DEFAULT now(),
  ai_started_at timestamptz,
  human_requested_at timestamptz,
  human_requested_by_type text CHECK (human_requested_by_type IS NULL OR human_requested_by_type IN ('agent', 'human', 'system')),
  human_requested_by_agent_id uuid REFERENCES public.agents(id) ON DELETE SET NULL,
  human_requested_by_user_id uuid REFERENCES public.users_v2(id) ON DELETE SET NULL,
  human_request_reason text,
  human_taken_at timestamptz,
  human_taken_by_user_id uuid REFERENCES public.users_v2(id) ON DELETE SET NULL,
  first_human_response_at timestamptz,
  returned_to_ai_at timestamptz,
  returned_to_ai_by_user_id uuid REFERENCES public.users_v2(id) ON DELETE SET NULL,
  resolved_at timestamptz,
  closed_at timestamptz,
  closed_by_type text CHECK (closed_by_type IS NULL OR closed_by_type IN ('human', 'agent', 'system')),
  closed_by_user_id uuid REFERENCES public.users_v2(id) ON DELETE SET NULL,
  closed_by_agent_id uuid REFERENCES public.agents(id) ON DELETE SET NULL,
  close_reason text,
  close_summary text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_attendance_sessions_conversation_started
  ON public.attendance_sessions(conversation_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_attendance_sessions_company_status
  ON public.attendance_sessions(company_id, status, started_at DESC);

-- No máximo uma sessão "viva" por conversa. returned_to_ai/resolved/closed são
-- terminais e ficam FORA do índice — um novo handoff cria nova sessão (§7.2).
CREATE UNIQUE INDEX IF NOT EXISTS uq_attendance_sessions_one_open_per_conversation
  ON public.attendance_sessions(conversation_id)
  WHERE status IN ('open', 'human_requested', 'human_active', 'pending_customer');

-- ----------------------------------------------------------------------------
-- §7.1 — FK deferrable de conversations -> attendance_sessions.
-- Criada DEPOIS de attendance_sessions existir, para não quebrar o ciclo.
-- DEFERRABLE INITIALLY DEFERRED: a checagem ocorre no commit, permitindo
-- ponteiro/linha serem escritos na mesma transação em qualquer ordem.
-- Guarda de idempotência: só adiciona se ainda não existe.
-- ----------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'conversations_current_attendance_session_fkey'
      AND conrelid = 'public.conversations'::regclass
  ) THEN
    ALTER TABLE public.conversations
      ADD CONSTRAINT conversations_current_attendance_session_fkey
      FOREIGN KEY (current_attendance_session_id)
      REFERENCES public.attendance_sessions(id)
      ON DELETE SET NULL
      DEFERRABLE INITIALLY DEFERRED;
  END IF;
END
$$;

-- ----------------------------------------------------------------------------
-- §7.3 — conversation_events (timeline imutável). Referencia attendance_sessions.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.conversation_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid NOT NULL REFERENCES public.conversations(id) ON DELETE CASCADE,
  attendance_session_id uuid REFERENCES public.attendance_sessions(id) ON DELETE SET NULL,
  company_id uuid NOT NULL REFERENCES public.companies(id) ON DELETE CASCADE,
  agent_id uuid REFERENCES public.agents(id) ON DELETE SET NULL,
  event_type text NOT NULL CHECK (event_type IN (
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
    'note_added'
  )),
  actor_type text CHECK (actor_type IS NULL OR actor_type IN ('customer', 'agent', 'human', 'system')),
  actor_user_id uuid REFERENCES public.users_v2(id) ON DELETE SET NULL,
  actor_agent_id uuid REFERENCES public.agents(id) ON DELETE SET NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  idempotency_key text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_conversation_events_conversation_created
  ON public.conversation_events(conversation_id, created_at DESC);

-- Retry-safe: eventos one-shot de ciclo de vida (handoff_requested, human_claimed,
-- returned_to_ai, resolved_*, closed_*, auto_close_scheduled, timeout_closed,
-- reopened_by_customer) recebem idempotency_key e não duplicam em retry da RPC.
-- Eventos repetíveis (ai_message_sent, customer_message_received, human_message_sent,
-- note_added) deixam idempotency_key NULL (índice parcial WHERE NOT NULL permite N).
CREATE UNIQUE INDEX IF NOT EXISTS uq_conversation_events_idempotency
  ON public.conversation_events(idempotency_key)
  WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_conversation_events_company_type_created
  ON public.conversation_events(company_id, event_type, created_at DESC);
