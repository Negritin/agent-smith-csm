-- ============================================================================
-- Sprint S1 — Atendimento/SLA/Handoff — agent_attendance_settings
-- SPEC-atendimento-sla-handoff-full.md §7.7.
--
-- Fonte de verdade das configs de atendimento por agente (handoff, auto-close,
-- mensagem de encerramento, reabertura). As flags que MATERIALIZAM tools
-- (handoff_enabled, agent_can_close) são espelhadas em agents.tools_config em
-- sprints posteriores (§9.3/§10.2) — aqui só criamos a tabela canônica.
--
-- agent_can_close DEFAULT false (§7.7 / S1).
-- PRINCÍPIO: aditivo e idempotente. RLS/grants em 20260621_99_rls_attendance_tables.sql.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.agent_attendance_settings (
  agent_id uuid PRIMARY KEY REFERENCES public.agents(id) ON DELETE CASCADE,
  company_id uuid NOT NULL REFERENCES public.companies(id) ON DELETE CASCADE,
  handoff_enabled boolean NOT NULL DEFAULT false,
  auto_close_enabled boolean NOT NULL DEFAULT false,
  auto_close_after_minutes int NOT NULL DEFAULT 240,
  auto_close_scope text NOT NULL DEFAULT 'all_attendance'
    CHECK (auto_close_scope IN ('all_attendance', 'human_only')),
  auto_close_message_enabled boolean NOT NULL DEFAULT true,
  auto_close_message text NOT NULL DEFAULT 'Encerramos este atendimento por falta de resposta. Se precisar, é só chamar novamente.',
  reopen_on_customer_reply boolean NOT NULL DEFAULT true,
  agent_can_close boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT agent_attendance_after_minutes_check CHECK (auto_close_after_minutes >= 5),
  CONSTRAINT agent_attendance_message_check CHECK (
    auto_close_message_enabled = false OR length(btrim(auto_close_message)) > 0
  )
);

CREATE INDEX IF NOT EXISTS idx_agent_attendance_settings_company
  ON public.agent_attendance_settings(company_id);
