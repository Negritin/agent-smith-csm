-- ============================================================================
-- CMA Sprint S4 — F2 Métricas: índice p/ filtros por created_at (SPEC §2.5).
--
-- Hoje só existem compostos em (company_id, status) e (company_id, last_message_at).
-- As queries de período de Métricas filtram conversations por (company_id, created_at)
-- → este índice. Arquivo SEPARADO e SEM BEGIN/COMMIT: CONCURRENTLY não roda dentro
-- de transação (falha 25001). IF NOT EXISTS = idempotente/reexecutável.
--
-- ⚠️ Em runners que abrem txn implícita (ex.: Supabase SQL Editor), trocar
--    CONCURRENTLY por CREATE INDEX simples (toma SHARE lock breve, rodar fora de pico),
--    espelhando o trade-off documentado em 20260621_90_concurrent_indexes.sql.
-- ⚠️ FILES ONLY: NÃO aplicar a banco vivo nesta sprint.
-- ============================================================================

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_conversations_company_created
  ON public.conversations (company_id, created_at);
