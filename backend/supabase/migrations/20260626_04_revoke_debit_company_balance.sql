-- FASE 0B / Sprint S2 — segurança: restringe debit_company_balance a service_role.
--
-- debit_company_balance é SECURITY DEFINER e estava com GRANT ALL para anon E authenticated
-- (schema_completo.sql:3583-3584) → qualquer cliente com a anon/authenticated key poderia
-- debitar saldo de qualquer empresa diretamente. Com bill_usage_group (20260626_03) como a
-- nova fonte do débito de consumo, restringimos a função antiga a service_role (backend/worker).
--
-- (Os callers legítimos de debit_company_balance rodam sob service_role.)
-- Rollback (combinado da FASE 0B): backend/supabase/rollbacks/20260626_billing_fase0b_rollback.sql

BEGIN;

-- IMPORTANTE: tem que revogar de PUBLIC também. Todo function nasce com GRANT EXECUTE
-- implícito a PUBLIC; anon/authenticated são MEMBROS de PUBLIC → revogar só deles NÃO
-- fecha o buraco (eles continuam executando via PUBLIC). Verificável:
--   SELECT has_function_privilege('anon','public.debit_company_balance(uuid,numeric)','EXECUTE'); -- deve ser false
REVOKE ALL ON FUNCTION public.debit_company_balance(uuid, numeric) FROM PUBLIC, anon, authenticated;
-- service_role mantém o GRANT (callers legítimos rodam sob service_role).

COMMIT;
