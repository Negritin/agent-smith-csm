-- ============================================================================
-- Sprint S1 — Atendimento/SLA/Handoff — sla_core
-- SPEC-atendimento-sla-handoff-full.md §7.4 (sla_policies), §7.5 (attendance_sla),
-- §7.6 (sla_events).
--
-- PRINCÍPIO: aditivo e idempotente. Depende de attendance_sessions e
-- conversations (20260621_01_attendance_core.sql) e sla_policies (este arquivo).
-- RLS/grants ficam em 20260621_99_rls_attendance_tables.sql.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- §7.4 — sla_policies. No máximo uma política ATIVA por empresa.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.sla_policies (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id uuid NOT NULL REFERENCES public.companies(id) ON DELETE CASCADE,
  name text NOT NULL DEFAULT 'Política padrão',
  is_active boolean NOT NULL DEFAULT true,
  timezone text NOT NULL DEFAULT 'America/Sao_Paulo',
  business_hours_enabled boolean NOT NULL DEFAULT false,
  working_days int[] NOT NULL DEFAULT ARRAY[1,2,3,4,5],
  working_start time,
  working_end time,
  normal_first_response_minutes int NOT NULL DEFAULT 15,
  normal_resolution_minutes int NOT NULL DEFAULT 240,
  high_first_response_minutes int NOT NULL DEFAULT 5,
  high_resolution_minutes int NOT NULL DEFAULT 120,
  critical_first_response_minutes int NOT NULL DEFAULT 2,
  critical_resolution_minutes int NOT NULL DEFAULT 60,
  default_sla_level text NOT NULL DEFAULT 'normal'
    CHECK (default_sla_level IN ('normal', 'high', 'critical')),
  created_by uuid REFERENCES public.users_v2(id) ON DELETE SET NULL,
  updated_by uuid REFERENCES public.users_v2(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT sla_policies_minutes_positive_check CHECK (
    normal_first_response_minutes > 0 AND normal_resolution_minutes > 0
    AND high_first_response_minutes > 0 AND high_resolution_minutes > 0
    AND critical_first_response_minutes > 0 AND critical_resolution_minutes > 0
  ),
  CONSTRAINT sla_policies_working_days_check CHECK (working_days <@ ARRAY[1,2,3,4,5,6,7]),
  CONSTRAINT sla_policies_business_hours_check CHECK (
    business_hours_enabled = false
    OR (working_start IS NOT NULL AND working_end IS NOT NULL AND working_start < working_end)
  )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_sla_policies_one_active_per_company
  ON public.sla_policies(company_id)
  WHERE is_active = true;

-- ----------------------------------------------------------------------------
-- §7.5 — attendance_sla. Snapshot do SLA aplicado à sessão de atendimento.
-- health_status (saúde atual) é independente de first_response_status e
-- resolution_status (marcos que não se sobrescrevem).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.attendance_sla (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  attendance_session_id uuid NOT NULL UNIQUE REFERENCES public.attendance_sessions(id) ON DELETE CASCADE,
  conversation_id uuid NOT NULL REFERENCES public.conversations(id) ON DELETE CASCADE,
  company_id uuid NOT NULL REFERENCES public.companies(id) ON DELETE CASCADE,
  policy_id uuid REFERENCES public.sla_policies(id) ON DELETE SET NULL,
  sla_level text NOT NULL CHECK (sla_level IN ('normal', 'high', 'critical')),
  health_status text NOT NULL DEFAULT 'within_sla'
    CHECK (health_status IN ('within_sla', 'at_risk', 'critical', 'breached', 'paused')),
  first_response_status text NOT NULL DEFAULT 'pending'
    CHECK (first_response_status IN ('pending', 'met', 'missed')),
  resolution_status text NOT NULL DEFAULT 'pending'
    CHECK (resolution_status IN ('pending', 'met', 'missed', 'breached')),
  started_at timestamptz NOT NULL,
  first_response_deadline timestamptz NOT NULL,
  first_response_at timestamptz,
  resolution_deadline timestamptz NOT NULL,
  resolved_at timestamptz,
  paused_at timestamptz,
  paused_duration_seconds int NOT NULL DEFAULT 0,
  policy_snapshot jsonb NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_attendance_sla_company_health_deadline
  ON public.attendance_sla(company_id, health_status, resolution_deadline);

CREATE INDEX IF NOT EXISTS idx_attendance_sla_first_response_deadline
  ON public.attendance_sla(company_id, first_response_deadline)
  WHERE first_response_status = 'pending';

-- ----------------------------------------------------------------------------
-- §7.6 — sla_events. Timeline detalhada de marcos de SLA. Marcos one-shot por
-- sessão têm unique parcial (não duplicam em retry do worker).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.sla_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  attendance_sla_id uuid REFERENCES public.attendance_sla(id) ON DELETE CASCADE,
  attendance_session_id uuid REFERENCES public.attendance_sessions(id) ON DELETE CASCADE,
  conversation_id uuid NOT NULL REFERENCES public.conversations(id) ON DELETE CASCADE,
  company_id uuid NOT NULL REFERENCES public.companies(id) ON DELETE CASCADE,
  event_type text NOT NULL CHECK (event_type IN (
    'sla_started',
    'first_response_met',
    'first_response_missed',
    'at_risk_50pct',
    'critical_75pct',
    'resolution_breached',
    'resolution_met',
    'resolution_missed',
    'sla_paused',
    'sla_resumed'
  )),
  actor_type text CHECK (actor_type IS NULL OR actor_type IN ('agent', 'human', 'system')),
  actor_user_id uuid REFERENCES public.users_v2(id) ON DELETE SET NULL,
  actor_agent_id uuid REFERENCES public.agents(id) ON DELETE SET NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_sla_events_once_per_session_type
  ON public.sla_events(attendance_session_id, event_type)
  WHERE event_type IN (
    'first_response_met',
    'first_response_missed',
    'at_risk_50pct',
    'critical_75pct',
    'resolution_breached',
    'resolution_met',
    'resolution_missed'
  );
