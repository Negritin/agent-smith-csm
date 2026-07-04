-- ============================================================================
-- Sprint S1 — Atendimento/SLA/Handoff — notifications_blocklist
-- SPEC-atendimento-sla-handoff-full.md §7.9.
--
-- Destinatários de alerta de handoff (handoff_notification_recipients),
-- blocklist de números internos para o guard de WhatsApp
-- (internal_whatsapp_blocklist, com block_count/last_blocked_at) e entregas
-- (notification_deliveries, com next_attempt_at/last_attempt_at/locked_until/
-- locked_by + idempotency_key unique).
--
-- PRINCÍPIO: aditivo e idempotente. Depende de companies, agents, integrations,
-- conversations, attendance_sessions e users_v2. RLS/grants em
-- 20260621_99_rls_attendance_tables.sql.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- §7.9 — handoff_notification_recipients
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.handoff_notification_recipients (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id uuid NOT NULL REFERENCES public.companies(id) ON DELETE CASCADE,
  agent_id uuid REFERENCES public.agents(id) ON DELETE CASCADE,
  channel text NOT NULL CHECK (channel IN ('email', 'whatsapp')),
  recipient_value text NOT NULL,
  recipient_normalized text NOT NULL,
  display_name text,
  enabled boolean NOT NULL DEFAULT true,
  created_by uuid REFERENCES public.users_v2(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_handoff_recipients_company_agent
  ON public.handoff_notification_recipients(company_id, agent_id, channel)
  WHERE enabled = true;

CREATE UNIQUE INDEX IF NOT EXISTS uq_handoff_recipient_active
  ON public.handoff_notification_recipients (
    company_id,
    coalesce(agent_id, '00000000-0000-0000-0000-000000000000'::uuid),
    channel,
    recipient_normalized
  )
  WHERE enabled = true;

-- ----------------------------------------------------------------------------
-- §7.9 — internal_whatsapp_blocklist (com block_count / last_blocked_at)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.internal_whatsapp_blocklist (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id uuid NOT NULL REFERENCES public.companies(id) ON DELETE CASCADE,
  agent_id uuid REFERENCES public.agents(id) ON DELETE CASCADE,
  integration_id uuid REFERENCES public.integrations(id) ON DELETE CASCADE,
  phone_normalized text NOT NULL,
  source_recipient_id uuid REFERENCES public.handoff_notification_recipients(id) ON DELETE SET NULL,
  reason text NOT NULL DEFAULT 'handoff_notification_recipient',
  active boolean NOT NULL DEFAULT true,
  block_count int NOT NULL DEFAULT 0,
  last_blocked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_internal_whatsapp_blocklist_scope
  ON public.internal_whatsapp_blocklist(
    company_id,
    phone_normalized,
    coalesce(agent_id, '00000000-0000-0000-0000-000000000000'::uuid),
    coalesce(integration_id, '00000000-0000-0000-0000-000000000000'::uuid)
  )
  WHERE active = true;

-- ----------------------------------------------------------------------------
-- §7.9 — notification_deliveries (com next_attempt_at / last_attempt_at /
-- locked_until / locked_by; idempotency_key unique global).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.notification_deliveries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id uuid NOT NULL REFERENCES public.companies(id) ON DELETE CASCADE,
  conversation_id uuid REFERENCES public.conversations(id) ON DELETE CASCADE,
  attendance_session_id uuid REFERENCES public.attendance_sessions(id) ON DELETE CASCADE,
  recipient_id uuid REFERENCES public.handoff_notification_recipients(id) ON DELETE SET NULL,
  event_type text NOT NULL,
  idempotency_key text NOT NULL,
  channel text NOT NULL CHECK (channel IN ('email', 'whatsapp')),
  recipient_value text NOT NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'sent', 'failed', 'skipped')),
  attempts int NOT NULL DEFAULT 0,
  next_attempt_at timestamptz,
  last_attempt_at timestamptz,
  locked_until timestamptz,
  locked_by text,
  provider_message_id text,
  last_error text,
  sent_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_delivery_idempotency
  ON public.notification_deliveries(idempotency_key);
