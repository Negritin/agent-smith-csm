-- ============================================================================
-- Sprint S1 — Atendimento/SLA/Handoff — rls_attendance_tables
-- SPEC-atendimento-sla-handoff-full.md §7.10 (Segurança, RLS, grants, tenant).
--
-- ENABLE RLS + REVOKE ALL FROM anon + GRANT ALL TO service_role nas 10 TABELAS
-- NOVAS, + policies `authenticated` escopadas por company_id.
--
-- ⚠️ ESCOPO: REVOKE de anon é feito SOMENTE nas 10 tabelas novas. NÃO tocamos
-- nos grants/policies de anon de conversations/messages nem na policy de
-- realtime de messages (isso é o Sprint S11, depois do polling estar pronto).
--
-- ⚠️ ORDENAÇÃO (§19 Fase 1 itens 2 e 6): este é o ÚLTIMO arquivo do S1
-- (prefixo `_99_`). O ENABLE RLS / REVOKE / GRANT / CREATE POLICY abaixo
-- referenciam TODAS as 10 tabelas novas, criadas pelos arquivos `_01_`..`_05_`.
-- O prefixo numérico garante (em ordem lexicográfica, como o Supabase CLI
-- aplica) que este RLS rode DEPOIS de todo o DDL de tabela; do contrário um
-- ENABLE RLS numa tabela ainda inexistente abortaria o deploy
-- (relation does not exist) e deixaria tabelas SLA sem RLS/REVOKE/policy.
--
-- ⚠️ CLAIMS / DEFESA EM PROFUNDIDADE: as policies `authenticated` assumem um
-- JWT Supabase (GoTrue/PostgREST) com claims `company_id`, `role` e `user_id`.
-- HOJE o projeto autentica via JWT interno HMAC (InternalJwtClaims em
-- backend/app/core/auth.py) + iron-session, e o backend acessa o banco como
-- service_role (BYPASS de RLS). Logo NENHUM caminho do S1 exercita estas
-- policies: a proteção EFETIVA do S1 é deny-by-default (RLS ON) + REVOKE anon +
-- ausência de GRANT de escrita a authenticated. Quando o caminho authenticated
-- for ativado: (1) confirmar que o JWT carrega `company_id`/`role`/`user_id`;
-- (2) alinhar os valores de `role` — users_v2.role no schema admite
-- 'admin_company'/'member' (schema_completo.sql), que NÃO batem com
-- 'admin'/'company_admin'/'master_admin' usados abaixo; incluir/normalizar o
-- valor canônico; (3) adicionar testes de RLS. Sem isso, as policies só fazem
-- over-restriction (fail-safe, não vazam).
--
-- ⚠️ CONSISTÊNCIA DE TENANT (§7.10 itens 5-6; §23 D1): a invariante
-- company_id == conversations.company_id NÃO é imposta no banco no S1 (sem
-- trigger nem CHECK cruzado; só FKs independentes p/ companies e conversations).
-- Por design, TODA escrita em attendance_*/sla_*/eventos/timers/notificações
-- passa pela RPC transacional da Fase 2 (service_role) que valida o tenant.
-- Nenhuma escrita direta por authenticated é permitida até lá (sem GRANT
-- INSERT/UPDATE/DELETE nem policy de escrita). Garantir essa validação na RPC
-- ou em trigger BEFORE INSERT/UPDATE antes de expor superfície de escrita.
--
-- CONVENÇÃO DE AUTORIZAÇÃO (alinhada às policies existentes do repo —
-- 20260528_sprint4_database_security.sql usa `(auth.jwt() ->> 'company_id')`):
--   - company scope: company_id = (auth.jwt() ->> 'company_id')::uuid
--   - company_admin (role 'admin'/'company_admin'): vê tudo da empresa
--   - membro comum: vê apenas conversas com assigned_user_id = auth.uid()
--     onde auth.uid() é mapeado de (auth.jwt() ->> 'user_id')::uuid
--   - team_id existe no schema, mas SEM policy de time (visibilidade por time
--     desativada enquanto não houver módulo de times — §7.10).
--
-- A coluna assigned_user_id vive em conversations, não nas tabelas de
-- atendimento. Por isso o filtro "membro comum" é resolvido via subquery em
-- conversations (escopada também por company_id, defesa em profundidade).
--
-- service_role faz BYPASS de RLS por padrão; ainda assim damos GRANT ALL
-- explícito (§7.10) para backend/BFF/workers.
--
-- PRINCÍPIO: idempotente — DROP POLICY IF EXISTS antes de CREATE POLICY;
-- ENABLE/REVOKE/GRANT são idempotentes por natureza.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- (1) ENABLE ROW LEVEL SECURITY — 10 tabelas novas (§7.10 template).
-- ----------------------------------------------------------------------------
ALTER TABLE public.attendance_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.conversation_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sla_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.attendance_sla ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sla_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_attendance_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.conversation_inactivity_timers ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.handoff_notification_recipients ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.internal_whatsapp_blocklist ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.notification_deliveries ENABLE ROW LEVEL SECURITY;

-- ----------------------------------------------------------------------------
-- (2) REVOKE ALL FROM anon — SOMENTE nas tabelas novas (§7.10 template).
-- ----------------------------------------------------------------------------
REVOKE ALL ON public.attendance_sessions FROM anon;
REVOKE ALL ON public.conversation_events FROM anon;
REVOKE ALL ON public.sla_policies FROM anon;
REVOKE ALL ON public.attendance_sla FROM anon;
REVOKE ALL ON public.sla_events FROM anon;
REVOKE ALL ON public.agent_attendance_settings FROM anon;
REVOKE ALL ON public.conversation_inactivity_timers FROM anon;
REVOKE ALL ON public.handoff_notification_recipients FROM anon;
REVOKE ALL ON public.internal_whatsapp_blocklist FROM anon;
REVOKE ALL ON public.notification_deliveries FROM anon;

-- ----------------------------------------------------------------------------
-- (3) GRANT ALL TO service_role — tabelas novas (§7.10 template).
-- ----------------------------------------------------------------------------
GRANT ALL ON public.attendance_sessions TO service_role;
GRANT ALL ON public.conversation_events TO service_role;
GRANT ALL ON public.sla_policies TO service_role;
GRANT ALL ON public.attendance_sla TO service_role;
GRANT ALL ON public.sla_events TO service_role;
GRANT ALL ON public.agent_attendance_settings TO service_role;
GRANT ALL ON public.conversation_inactivity_timers TO service_role;
GRANT ALL ON public.handoff_notification_recipients TO service_role;
GRANT ALL ON public.internal_whatsapp_blocklist TO service_role;
GRANT ALL ON public.notification_deliveries TO service_role;

-- ----------------------------------------------------------------------------
-- (4) GRANTs mínimos a authenticated (RLS ainda filtra linha a linha).
-- SELECT em todas; escrita fica a cargo de RPCs/service_role no backend.
-- ----------------------------------------------------------------------------
GRANT SELECT ON public.attendance_sessions TO authenticated;
GRANT SELECT ON public.conversation_events TO authenticated;
GRANT SELECT ON public.sla_policies TO authenticated;
GRANT SELECT ON public.attendance_sla TO authenticated;
GRANT SELECT ON public.sla_events TO authenticated;
GRANT SELECT ON public.agent_attendance_settings TO authenticated;
GRANT SELECT ON public.conversation_inactivity_timers TO authenticated;
GRANT SELECT ON public.handoff_notification_recipients TO authenticated;
GRANT SELECT ON public.internal_whatsapp_blocklist TO authenticated;
GRANT SELECT ON public.notification_deliveries TO authenticated;

-- ----------------------------------------------------------------------------
-- (5) Policies `authenticated` escopadas por company_id.
--
-- Predicado de visibilidade reutilizado:
--   company_id = (auth.jwt() ->> 'company_id')::uuid
--   AND (
--     (auth.jwt() ->> 'role') IN ('admin','company_admin','master_admin')  -- vê tudo da empresa
--     OR <conversa atribuída ao próprio usuário>
--   )
--
-- Para tabelas SEM conversation_id próprio (sla_policies,
-- agent_attendance_settings, handoff_notification_recipients,
-- internal_whatsapp_blocklist) o escopo é só company_admin: são configs da
-- empresa, não atribuíveis a membro comum.
-- ----------------------------------------------------------------------------

-- attendance_sessions: admin vê tudo da empresa; membro vê só conversas suas.
DROP POLICY IF EXISTS attendance_sessions_company_scope ON public.attendance_sessions;
CREATE POLICY attendance_sessions_company_scope ON public.attendance_sessions
  FOR SELECT TO authenticated
  USING (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND (
      (auth.jwt() ->> 'role') IN ('admin', 'company_admin', 'master_admin')
      OR EXISTS (
        SELECT 1 FROM public.conversations c
        WHERE c.id = attendance_sessions.conversation_id
          AND c.company_id = (auth.jwt() ->> 'company_id')::uuid
          AND c.assigned_user_id = (auth.jwt() ->> 'user_id')::uuid
      )
    )
  );

-- conversation_events
DROP POLICY IF EXISTS conversation_events_company_scope ON public.conversation_events;
CREATE POLICY conversation_events_company_scope ON public.conversation_events
  FOR SELECT TO authenticated
  USING (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND (
      (auth.jwt() ->> 'role') IN ('admin', 'company_admin', 'master_admin')
      OR EXISTS (
        SELECT 1 FROM public.conversations c
        WHERE c.id = conversation_events.conversation_id
          AND c.company_id = (auth.jwt() ->> 'company_id')::uuid
          AND c.assigned_user_id = (auth.jwt() ->> 'user_id')::uuid
      )
    )
  );

-- attendance_sla
DROP POLICY IF EXISTS attendance_sla_company_scope ON public.attendance_sla;
CREATE POLICY attendance_sla_company_scope ON public.attendance_sla
  FOR SELECT TO authenticated
  USING (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND (
      (auth.jwt() ->> 'role') IN ('admin', 'company_admin', 'master_admin')
      OR EXISTS (
        SELECT 1 FROM public.conversations c
        WHERE c.id = attendance_sla.conversation_id
          AND c.company_id = (auth.jwt() ->> 'company_id')::uuid
          AND c.assigned_user_id = (auth.jwt() ->> 'user_id')::uuid
      )
    )
  );

-- sla_events
DROP POLICY IF EXISTS sla_events_company_scope ON public.sla_events;
CREATE POLICY sla_events_company_scope ON public.sla_events
  FOR SELECT TO authenticated
  USING (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND (
      (auth.jwt() ->> 'role') IN ('admin', 'company_admin', 'master_admin')
      OR EXISTS (
        SELECT 1 FROM public.conversations c
        WHERE c.id = sla_events.conversation_id
          AND c.company_id = (auth.jwt() ->> 'company_id')::uuid
          AND c.assigned_user_id = (auth.jwt() ->> 'user_id')::uuid
      )
    )
  );

-- conversation_inactivity_timers
DROP POLICY IF EXISTS conversation_inactivity_timers_company_scope ON public.conversation_inactivity_timers;
CREATE POLICY conversation_inactivity_timers_company_scope ON public.conversation_inactivity_timers
  FOR SELECT TO authenticated
  USING (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND (
      (auth.jwt() ->> 'role') IN ('admin', 'company_admin', 'master_admin')
      OR EXISTS (
        SELECT 1 FROM public.conversations c
        WHERE c.id = conversation_inactivity_timers.conversation_id
          AND c.company_id = (auth.jwt() ->> 'company_id')::uuid
          AND c.assigned_user_id = (auth.jwt() ->> 'user_id')::uuid
      )
    )
  );

-- notification_deliveries (conversation_id é nullable; quando nulo, só admin vê)
DROP POLICY IF EXISTS notification_deliveries_company_scope ON public.notification_deliveries;
CREATE POLICY notification_deliveries_company_scope ON public.notification_deliveries
  FOR SELECT TO authenticated
  USING (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND (
      (auth.jwt() ->> 'role') IN ('admin', 'company_admin', 'master_admin')
      OR EXISTS (
        SELECT 1 FROM public.conversations c
        WHERE c.id = notification_deliveries.conversation_id
          AND c.company_id = (auth.jwt() ->> 'company_id')::uuid
          AND c.assigned_user_id = (auth.jwt() ->> 'user_id')::uuid
      )
    )
  );

-- sla_policies: config da empresa — só company_admin.
DROP POLICY IF EXISTS sla_policies_admin_scope ON public.sla_policies;
CREATE POLICY sla_policies_admin_scope ON public.sla_policies
  FOR SELECT TO authenticated
  USING (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND (auth.jwt() ->> 'role') IN ('admin', 'company_admin', 'master_admin')
  );

-- agent_attendance_settings: config do agente — só company_admin.
DROP POLICY IF EXISTS agent_attendance_settings_admin_scope ON public.agent_attendance_settings;
CREATE POLICY agent_attendance_settings_admin_scope ON public.agent_attendance_settings
  FOR SELECT TO authenticated
  USING (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND (auth.jwt() ->> 'role') IN ('admin', 'company_admin', 'master_admin')
  );

-- handoff_notification_recipients: destinatários de alerta — só company_admin.
DROP POLICY IF EXISTS handoff_notification_recipients_admin_scope ON public.handoff_notification_recipients;
CREATE POLICY handoff_notification_recipients_admin_scope ON public.handoff_notification_recipients
  FOR SELECT TO authenticated
  USING (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND (auth.jwt() ->> 'role') IN ('admin', 'company_admin', 'master_admin')
  );

-- internal_whatsapp_blocklist: números internos — só company_admin.
DROP POLICY IF EXISTS internal_whatsapp_blocklist_admin_scope ON public.internal_whatsapp_blocklist;
CREATE POLICY internal_whatsapp_blocklist_admin_scope ON public.internal_whatsapp_blocklist
  FOR SELECT TO authenticated
  USING (
    company_id = (auth.jwt() ->> 'company_id')::uuid
    AND (auth.jwt() ->> 'role') IN ('admin', 'company_admin', 'master_admin')
  );
