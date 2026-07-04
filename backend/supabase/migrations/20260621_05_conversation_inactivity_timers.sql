-- ============================================================================
-- Sprint S1 — Atendimento/SLA/Handoff — conversation_inactivity_timers
-- SPEC-atendimento-sla-handoff-full.md §7.8.
--
-- Agendamento de auto-close por inatividade. No máximo um timer ATIVO por
-- (conversation_id, timer_type) — unique parcial uq_inactivity_timers_one_scheduled.
--
-- CICLO DE VIDA do status:
--   scheduled  → agendado, aguardando next_action_at vencer;
--   processing → reivindicado por UMA tick do worker via claim atômico (CAS:
--                UPDATE ... SET status='processing' WHERE id=? AND status='scheduled');
--                garante single-winner contra ticks concorrentes (dois beats,
--                beat + rota de contingência inline, autoretry + beat);
--   cancelled  → cliente respondeu / transição de atendimento cancelou o timer;
--   executed   → auto-close concluído (conversa fechada via close_by_system);
--   failed     → o fechamento falhou (error_message preenchido).
--
-- SEMÂNTICA DO UNIQUE PARCIAL (uq_inactivity_timers_one_scheduled): um timer em
-- 'processing' AINDA é o timer ATIVO da conversa (a tick que o reivindicou está
-- justamente fechando a conversa). Portanto o unique cobre ('scheduled',
-- 'processing'): enquanto um timer processa, um novo schedule NÃO pode ser criado
-- para a mesma (conversation_id, timer_type) — isso evita um timer duplicado
-- nascendo durante o processamento. Quando o timer termina (executed/failed/
-- cancelled) ele sai do escopo do unique e um novo schedule é permitido.
--
-- PRINCÍPIO: aditivo e idempotente. Depende de attendance_sessions, conversations
-- e messages. RLS/grants em 20260621_99_rls_attendance_tables.sql.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.conversation_inactivity_timers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid NOT NULL REFERENCES public.conversations(id) ON DELETE CASCADE,
  attendance_session_id uuid REFERENCES public.attendance_sessions(id) ON DELETE CASCADE,
  company_id uuid NOT NULL REFERENCES public.companies(id) ON DELETE CASCADE,
  agent_id uuid REFERENCES public.agents(id) ON DELETE SET NULL,
  timer_type text NOT NULL DEFAULT 'auto_close'
    CHECK (timer_type IN ('auto_close')),
  status text NOT NULL DEFAULT 'scheduled'
    CHECK (status IN ('scheduled', 'processing', 'cancelled', 'executed', 'failed')),
  basis_message_id uuid REFERENCES public.messages(id) ON DELETE SET NULL,
  basis_at timestamptz NOT NULL,
  next_action_at timestamptz NOT NULL,
  executed_at timestamptz,
  cancelled_at timestamptz,
  error_message text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_inactivity_timers_due
  ON public.conversation_inactivity_timers(next_action_at)
  WHERE status = 'scheduled';

CREATE INDEX IF NOT EXISTS idx_inactivity_timers_conversation_status
  ON public.conversation_inactivity_timers(conversation_id, status);

-- Um único timer ATIVO por (conversation_id, timer_type). 'processing' conta como
-- ativo (ver header): enquanto a tick que reivindicou o timer fecha a conversa,
-- nenhum novo 'scheduled' pode nascer para a mesma conversa — sem timer duplicado.
CREATE UNIQUE INDEX IF NOT EXISTS uq_inactivity_timers_one_scheduled
  ON public.conversation_inactivity_timers(conversation_id, timer_type)
  WHERE status IN ('scheduled', 'processing');
