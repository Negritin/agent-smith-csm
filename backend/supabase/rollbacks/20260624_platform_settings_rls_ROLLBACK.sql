-- ============================================================================
-- ROLLBACK de 20260624_platform_settings_rls.sql (ALTO-002) — passo MANUAL.
--
-- LOCALIZACAO PROPOSITAL: este arquivo NAO esta em backend/supabase/migrations/
-- e NAO deve ser movido para la. O runner (Supabase CLI) aplica TODOS os .sql de
-- migrations/ em ordem lexicografica; se este rollback vivesse la, ordenaria logo
-- apos o forward (..._rls.sql < ..._rls_ROLLBACK.sql) e DESFARIA o saneamento no
-- MESMO deploy, reabrindo o vazamento de config global.
--
-- COMO APLICAR (incidente, nunca automatico):
--   psql "$DATABASE_URL" -f backend/supabase/rollbacks/20260624_platform_settings_rls_ROLLBACK.sql
--
-- QUANDO USAR: se, apos o REVOKE, algum caminho legitimo NAO mapeado na auditoria
-- (esperado: nenhum — todo acesso e service_role via backend) parar de funcionar
-- com "permission denied for table platform_settings". Restaura o estado de
-- pre-migration (RLS desligada + grants amplos de Supabase) e investigue o caller
-- antes de tentar o REVOKE de novo.
--
-- ATENCAO: este rollback REINTRODUZ o risco ALTO-002 (anon/authenticated leem e
-- sobrescrevem o system_base_prompt global). Use apenas como mitigacao temporaria
-- de incidente.
--
-- SIMETRIA: reverte EXATAMENTE o forward — desliga RLS e re-concede os grants
-- amplos que o Supabase concede por padrao a anon/authenticated em public.
-- GRANT/ALTER sao idempotentes; seguro re-rodar.
-- ============================================================================

BEGIN;

-- (1) Desliga o deny-by-default.
ALTER TABLE public.platform_settings DISABLE ROW LEVEL SECURITY;

-- (2) Re-concede os grants amplos de Supabase (estado de pre-migration).
GRANT ALL ON public.platform_settings TO anon, authenticated;

-- (service_role ja tinha GRANT ALL; mantido por simetria/idempotencia.)
GRANT ALL ON public.platform_settings TO service_role;

COMMIT;
