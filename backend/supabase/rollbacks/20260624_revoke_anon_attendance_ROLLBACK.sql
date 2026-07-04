-- ============================================================================
-- Sprint S11 — Atendimento/SLA/Handoff — revoke_anon_attendance ROLLBACK
-- SPEC-atendimento-sla-handoff-full.md §17 / §24 (rollback imediato de incidente).
--
-- LOCALIZAÇÃO PROPOSITAL: este arquivo NÃO está em `backend/supabase/migrations/`
-- e NÃO deve ser movido para lá. O runner (Supabase CLI: `supabase db push` /
-- `supabase migration up`) aplica TODOS os `.sql` de `migrations/` em ordem
-- lexicográfica e NÃO exclui sufixos `_ROLLBACK`; se este arquivo vivesse no
-- diretório de migrations, ordenaria logo após o forward REVOKE
-- (`...revoke_anon_attendance.sql` < `...revoke_anon_attendance_ROLLBACK.sql`) e
-- DESFARIA silenciosamente o saneamento no MESMO deploy (re-GRANT ALL + policy
-- de realtime aberta de volta), reintroduzindo o risco crítico que o S11 fecha.
--
-- COMO APLICAR (passo MANUAL de incidente, nunca automático):
--   psql "$DATABASE_URL" -f backend/supabase/rollbacks/20260624_revoke_anon_attendance_ROLLBACK.sql
--
-- REVERTE EXATAMENTE `../migrations/20260624_revoke_anon_attendance.sql`:
--   * re-GRANT ALL ON public.conversations TO anon   (estado de schema_completo.sql:3781)
--   * re-GRANT ALL ON public.messages      TO anon   (estado de schema_completo.sql:3880)
--   * recria a policy "Allow realtime subscriptions on messages" ... TO anon USING(true)
--                                                    (estado de schema_completo.sql:3007)
--
-- QUANDO USAR: se, após o REVOKE, o admin OU o widget pararem de atualizar (ou
-- seja, se algum caminho ainda depender da subscription `anon` de `messages` /
-- da leitura ampla `anon`), aplique este rollback para restaurar o tempo-real
-- imediatamente e volte ao polling (S9) antes de tentar o REVOKE de novo.
--
-- IDEMPOTÊNCIA: `GRANT` é idempotente (re-conceder é no-op). A recriação da
-- policy é protegida por `DROP POLICY IF EXISTS` antes do `CREATE POLICY`.
--
-- SIMETRIA: este arquivo re-concede EXATAMENTE as duas tabelas revogadas e
-- recria a EXATA policy removida — nada além disso. NÃO toca grants de RPC do
-- widget (que a migration de REVOKE também não tocou).
-- ============================================================================

BEGIN;

-- (1) Re-conceder a leitura/escrita ampla de `anon` (estado original do schema).
GRANT ALL ON public.conversations TO anon;
GRANT ALL ON public.messages TO anon;

-- (2) Recriar a policy de realtime anônima de `messages` (idempotente).
DROP POLICY IF EXISTS "Allow realtime subscriptions on messages" ON public.messages;
CREATE POLICY "Allow realtime subscriptions on messages"
  ON public.messages FOR SELECT TO anon USING (true);

COMMIT;
