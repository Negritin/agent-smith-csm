-- ============================================================================
-- Sprint S1 — Atendimento/SLA/Handoff — messages_company_id
-- SPEC-atendimento-sla-handoff-full.md §7.1 + §17 item 5.
--
-- Migration SEPARADA (§17 item 5): adiciona messages.company_id nullable, faz
-- backfill EM LOTE a partir de conversations.company_id e cria o índice de
-- company_id SOMENTE APÓS o backfill.
--
-- PRINCÍPIO: aditivo, idempotente, nullable (permanece nullable — §S1 critério
-- 133/145). NÃO toca grants/policies de anon em messages (S11).
-- ============================================================================

-- ----------------------------------------------------------------------------
-- Coluna company_id (nullable, sem default) em messages.
-- ----------------------------------------------------------------------------
ALTER TABLE public.messages
  ADD COLUMN IF NOT EXISTS company_id uuid;

-- ----------------------------------------------------------------------------
-- Backfill EM LOTE (chunks de 5000) a partir de conversations.company_id.
-- Só atualiza linhas com company_id ainda nulo e cuja conversa tem company_id.
--
-- COMPATIBILIDADE COM RUNNER TRANSACIONAL (correção do erro 2D000):
-- UPDATE SIMPLES e IDEMPOTENTE — NÃO uma PROCEDURE com COMMIT (COMMIT em
-- procedure falha com "2D000: invalid transaction termination" quando o runner
-- envolve o arquivo numa transação, ex.: Supabase SQL Editor). Roda numa única
-- transação e funciona em qualquer runner.
--
-- Idempotência: só toca linhas com company_id IS NULL (reexecutável).
-- ⚠️ VOLUME: em `messages` muito grande/quente, roda numa transação única e pode
-- segurar locks durante a execução — aplicar FORA DE PICO. company_id é NULLABLE
-- e pode ser adiado/segmentado sem quebrar (RLS de messages não está ativa). Para
-- base enorme, fazer em lotes via psql autocommit. Ver RUNBOOK §2.
-- ----------------------------------------------------------------------------
UPDATE public.messages m
  SET company_id = c.company_id
  FROM public.conversations c
  WHERE m.conversation_id = c.id
    AND m.company_id IS NULL
    AND c.company_id IS NOT NULL;

-- ----------------------------------------------------------------------------
-- Índice de company_id: MOVIDO para 20260621_90_concurrent_indexes.sql como
-- CREATE INDEX CONCURRENTLY (§24). `messages` é a tabela mais quente do banco;
-- um CREATE INDEX simples pegaria SHARE lock e bloquearia o ingest de mensagens
-- (webhook WhatsApp/chat) durante todo o build. O `_90_` roda DEPOIS deste
-- arquivo na ordem lexicográfica, então o backfill de company_id acima já estará
-- concluído quando o índice for criado. company_id permanece NULLABLE.
-- ----------------------------------------------------------------------------
