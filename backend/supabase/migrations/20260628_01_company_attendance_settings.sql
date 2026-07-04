-- ============================================================================
-- Atendimento — MOVE auto-close (inatividade) do AGENTE para a EMPRESA.
--
-- Decisão do dono: o "Encerramento automático por inatividade" (auto_close_*)
-- deixa de ser por-agente e passa a ser config da EMPRESA (company-level). As
-- flags reopen_on_customer_reply e agent_can_close PERMANECEM no agente
-- (agent_attendance_settings) — esta migration NÃO dropa colunas de lá.
--
-- Cria public.company_attendance_settings (PK = company_id), espelhando o
-- cabeçalho de segurança/RLS de 20260621_04_agent_attendance_settings.sql +
-- 20260621_99_rls_attendance_tables.sql: config da empresa → escopo só
-- company_admin (mesma convenção de sla_policies/agent_attendance_settings).
--
-- BACKFILL idempotente: para cada company que já tem agent_attendance_settings,
-- copia auto_close_* de UMA linha de agente — preferindo a que tem
-- auto_close_enabled=true (DISTINCT ON ... ORDER BY company_id,
-- auto_close_enabled DESC). ON CONFLICT DO NOTHING torna o re-run seguro.
--
-- PRINCÍPIO: aditivo e idempotente. CREATE TABLE IF NOT EXISTS, DROP POLICY IF
-- EXISTS antes de CREATE POLICY; ENABLE/REVOKE/GRANT são idempotentes.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- (1) Tabela canônica — config de auto-close por EMPRESA.
--     auto_close_message DEFAULT é idêntica à de agent_attendance_settings
--     (20260621_04) para que a migração de nível não altere o texto enviado.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.company_attendance_settings (
  company_id uuid PRIMARY KEY REFERENCES public.companies(id) ON DELETE CASCADE,
  auto_close_enabled boolean NOT NULL DEFAULT false,
  auto_close_after_minutes integer NOT NULL DEFAULT 240
    CHECK (auto_close_after_minutes >= 5),
  auto_close_scope text NOT NULL DEFAULT 'all_attendance'
    CHECK (auto_close_scope IN ('all_attendance', 'human_only')),
  auto_close_message_enabled boolean NOT NULL DEFAULT true,
  auto_close_message text NOT NULL DEFAULT 'Encerramos este atendimento por falta de resposta. Se precisar, é só chamar novamente.',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT company_attendance_message_check CHECK (
    auto_close_message_enabled = false OR length(btrim(auto_close_message)) > 0
  )
);

-- ----------------------------------------------------------------------------
-- (2) BACKFILL idempotente a partir de agent_attendance_settings.
--     DISTINCT ON (company_id) com ORDER BY auto_close_enabled DESC escolhe, por
--     empresa, a linha com auto-close LIGADO quando houver (preserva a intenção
--     do operador). ON CONFLICT DO NOTHING não toca empresas já migradas.
-- ----------------------------------------------------------------------------
INSERT INTO public.company_attendance_settings (
  company_id,
  auto_close_enabled,
  auto_close_after_minutes,
  auto_close_scope,
  auto_close_message_enabled,
  auto_close_message
)
SELECT DISTINCT ON (aas.company_id)
  aas.company_id,
  aas.auto_close_enabled,
  aas.auto_close_after_minutes,
  aas.auto_close_scope,
  aas.auto_close_message_enabled,
  aas.auto_close_message
FROM public.agent_attendance_settings aas
ORDER BY aas.company_id, aas.auto_close_enabled DESC
ON CONFLICT (company_id) DO NOTHING;

-- ----------------------------------------------------------------------------
-- (3) RLS / grants — espelham agent_attendance_settings (config da empresa →
--     só company_admin). service_role faz BYPASS de RLS (backend/BFF/workers),
--     mas damos GRANT ALL explícito como nas demais tabelas de atendimento.
-- ----------------------------------------------------------------------------
ALTER TABLE public.company_attendance_settings ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.company_attendance_settings FROM anon;

GRANT ALL ON public.company_attendance_settings TO service_role;
GRANT SELECT ON public.company_attendance_settings TO authenticated;

-- config da empresa — só company_admin (mesma convenção de
-- agent_attendance_settings_admin_scope / sla_policies_admin_scope).
DROP POLICY IF EXISTS company_attendance_settings_admin_scope ON public.company_attendance_settings;
CREATE POLICY company_attendance_settings_admin_scope ON public.company_attendance_settings
  FOR SELECT TO authenticated
  USING (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND (auth.jwt() ->> 'role') IN ('admin', 'company_admin', 'master_admin')
  );
