-- ============================================================================
-- ROLLBACK de 20260624_mcp_oauth_clients_rls.sql (MEDIO-005) — passo MANUAL.
--
-- LOCALIZACAO PROPOSITAL: este arquivo NAO esta em backend/supabase/migrations/
-- e NAO deve ser movido para la. O runner (Supabase CLI) aplica TODOS os .sql de
-- migrations/ em ordem lexicografica; se este rollback vivesse la, ordenaria logo
-- apos o forward (..._rls.sql < ..._rls_ROLLBACK.sql) e DESFARIA o saneamento no
-- MESMO deploy, reabrindo o vazamento dos segredos OAuth.
--
-- COMO APLICAR (incidente, nunca automatico):
--   psql "$DATABASE_URL" -f backend/supabase/rollbacks/20260624_mcp_oauth_clients_rls_ROLLBACK.sql
--
-- QUANDO USAR: se, apos o REVOKE, o fluxo OAuth/DCR dos MCPs remotos parar
-- (esperado: nao para — roda 100% como service_role) com "permission denied for
-- table mcp_oauth_clients". Restaura o estado de pre-migration e investigue antes
-- de tentar o REVOKE de novo.
--
-- ATENCAO: este rollback REINTRODUZ o risco MEDIO-005 (anon/authenticated
-- alcancam client_secret/registration_access_token via PostgREST). Use apenas
-- como mitigacao temporaria de incidente.
--
-- SIMETRIA: reverte EXATAMENTE o forward — desliga RLS e re-concede os grants
-- amplos que o Supabase concede por padrao a anon/authenticated em public.
-- GRANT/ALTER sao idempotentes; seguro re-rodar.
-- ============================================================================

BEGIN;

-- (1) Desliga o deny-by-default.
ALTER TABLE public.mcp_oauth_clients DISABLE ROW LEVEL SECURITY;

-- (2) Re-concede os grants amplos de Supabase (estado de pre-migration).
GRANT ALL ON public.mcp_oauth_clients TO anon, authenticated;

-- (service_role ja tinha GRANT ALL; mantido por simetria/idempotencia.)
GRANT ALL ON public.mcp_oauth_clients TO service_role;

COMMIT;
