-- ============================================================================
-- Sprint S8 — Atendimento/SLA/Handoff — índices da VARREDURA do worker de SLA
-- SPEC-atendimento-sla-handoff-full.md §15 (worker de SLA roda a cada 60s).
--
-- Usa CREATE INDEX SIMPLES (NÃO CONCURRENTLY) — roda em qualquer runner, incl. o
-- Supabase SQL Editor (CONCURRENTLY falha com 25001 em transação). Como
-- attendance_sla é tabela NOVA e ainda VAZIA neste deploy, o build é instantâneo
-- e SEM lock relevante — não há trade-off aqui. IF NOT EXISTS = idempotente.
--
-- PROBLEMA: a varredura GLOBAL do worker (sem company_id) não casava nenhum dos
-- índices de 20260621_02_sla_core.sql, que lideram por company_id (otimizados
-- para a UI por-tenant). Resultado: cada tick fazia full scan / partial-index
-- scan de TODA a attendance_sla pendente, com custo crescendo linearmente:
--
--   (1) varredura de thresholds: resolution_status='pending' AND health_status<>'paused'
--       → sem índice em resolution_status → Seq Scan.
--   (2) varredura de first-response: first_response_status='pending' AND
--       first_response_deadline<=now() → o índice existente lidera por company_id,
--       que o worker OMITE → não há range-seek por deadline.
--
-- PRINCÍPIO: aditivo e idempotente (CREATE INDEX IF NOT EXISTS).
-- Estes índices servem a varredura GLOBAL do worker; os índices por-tenant da §7.5
-- continuam existindo para a UI. Confirmar com EXPLAIN que o worker usa estes
-- índices em vez de Seq Scan.
-- ============================================================================

-- (1) Sweep de thresholds (§15): chave por resolution_deadline (worker ordena por
--     mais urgente primeiro) restrita às linhas pendentes. health_status incluído
--     para o filtro neq('paused') ser resolvido pelo índice.
CREATE INDEX IF NOT EXISTS idx_attendance_sla_pending_resolution
  ON public.attendance_sla(resolution_deadline, health_status)
  WHERE resolution_status = 'pending';

-- (2) Sweep de first-response (§15): chave por first_response_deadline (range-seek
--     <= now()), SEM company_id na frente (a varredura é global). Espelha o índice
--     por-tenant da §7.5 mas com a coluna de acesso do worker como líder.
CREATE INDEX IF NOT EXISTS idx_attendance_sla_worker_first_response
  ON public.attendance_sla(first_response_deadline)
  WHERE first_response_status = 'pending';
