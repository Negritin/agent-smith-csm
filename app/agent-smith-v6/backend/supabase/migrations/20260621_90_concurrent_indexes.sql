-- ============================================================================
-- Sprint S1 — Atendimento/SLA/Handoff — concurrent_indexes
-- SPEC-atendimento-sla-handoff-full.md §7.1 (índices de conversations) + §24.
--
-- Índices em conversations (já populada) + messages.company_id. Usa CREATE INDEX
-- SIMPLES (NÃO CONCURRENTLY) para rodar em QUALQUER runner, incl. o Supabase SQL
-- Editor — CONCURRENTLY falha com 25001 dentro de transação. IF NOT EXISTS =
-- idempotente/reexecutável. Este arquivo é o `_90_`: roda DEPOIS de todo o DDL e
-- backfill (tabelas/colunas/constraints/VALIDATE/_06/_07) e ANTES do RLS (`_99_`).
--
-- ⚠️ TRADE-OFF DE LOCK: CREATE INDEX não-concorrente toma SHARE lock em
-- conversations/messages (bloqueia ESCRITA, permite leitura) durante o build —
-- aplicar FORA DE PICO. Em tabelas MUITO grandes, criar estes índices à parte
-- via psql/CLI com CONCURRENTLY (autocommit), ou aceitar o lock breve aqui.
--
-- Depende de 20260621_01_attendance_core.sql (coluna current_attendance_session_id)
-- e de 20260621_07_messages_company_id.sql (coluna + backfill de messages.company_id,
-- que rodam ANTES deste arquivo na ordem lexicográfica).
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_conversations_company_status_last_message
  ON public.conversations(company_id, status, last_message_at DESC);

CREATE INDEX IF NOT EXISTS idx_conversations_company_assigned_status
  ON public.conversations(company_id, assigned_user_id, status);

CREATE INDEX IF NOT EXISTS idx_conversations_company_agent_status
  ON public.conversations(company_id, agent_id, status, last_message_at DESC);

CREATE INDEX IF NOT EXISTS idx_conversations_current_attendance_session
  ON public.conversations(current_attendance_session_id);

-- Índice de messages.company_id (movido de 20260621_07_*). `messages` é a tabela
-- mais quente do banco; CONCURRENTLY evita bloquear o ingest de mensagens durante
-- o build. O backfill de company_id (em _07_) já concluiu nesta ordem.
CREATE INDEX IF NOT EXISTS idx_messages_company_id
  ON public.messages(company_id);
