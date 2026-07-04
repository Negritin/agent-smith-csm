-- ============================================================================
-- Sprint S5 — Atendimento/SLA/Handoff — SWAP de unicidade de session_id
-- SPEC-atendimento-sla-handoff-full.md §7.1 (swap de unicidade, ordem
-- obrigatória) + §10.1 + §22 + §23 (ordem de segurança) + §24 (registro de risco).
--
-- O QUE FAZ:
--   Troca a unicidade GLOBAL de conversations.session_id
--   (constraint legada `conversations_session_id_key`) pela unicidade
--   MULTI-TENANT (company_id, coalesce(agent_id, ...), session_id).
--
-- ⚠️ ORDEM OBRIGATÓRIA (NÃO-NEGOCIÁVEL — §7.1/§10.1/§23):
--   Esta migration SÓ pode ser aplicada DEPOIS de a handoff tool
--   (request_human_agent) ser AUTO-DEFENSIVA — i.e. escopar toda escrita por
--   session_id + company_id + agent_id (AttendanceService.request_handoff via
--   RPC transacional única). Isso já foi entregue NESTE MESMO sprint (S5) ANTES
--   desta migration: backend/app/agents/tools/human_handoff.py reescrito.
--   Dropar `conversations_session_id_key` ANTES de a tool ser tenant-safe abriria
--   uma janela de update cross-tenant. Por isso o swap está no MESMO deploy,
--   DEPOIS do código tenant-safe.
--
-- RUNNER: usa CREATE UNIQUE INDEX SIMPLES (NÃO CONCURRENTLY) — roda em QUALQUER
--   runner, incl. o Supabase SQL Editor (CONCURRENTLY falha com 25001 dentro de
--   transação). Idempotente: CREATE UNIQUE INDEX IF NOT EXISTS + DROP ... IF EXISTS.
--   Prefixo 20260623_03 garante ordem DEPOIS do S2 (20260622_*).
--
-- ⚠️ TRADE-OFF DE LOCK: `conversations` é a tabela MAIS QUENTE (ingest WhatsApp +
--   /chat). CREATE UNIQUE INDEX não-concorrente toma SHARE lock (bloqueia ESCRITA,
--   permite leitura) durante o build — aplicar FORA DE PICO. Em base MUITO grande,
--   criar o índice à parte via psql/CLI com CONCURRENTLY (autocommit) e só então
--   dropar a constraint global.
--
-- ORDEM DOS STATEMENTS (sem janela sem unicidade): cria o NOVO índice unique
--   ANTES de dropar a constraint global legada. Num runner transacional (SQL
--   Editor) o CREATE + DROP são ATÔMICOS — nunca há estado committed sem unicidade.
--   Se o CREATE falhar (ex.: colisão), o DROP não roda e a unicidade global
--   permanece intacta — falha segura.
--
-- ⚠️ PRÉ-FLIGHT DE DUPLICIDADE OBRIGATÓRIO (§7.1/§24):
--   O `CREATE UNIQUE INDEX` abaixo FALHA e BLOQUEIA o deploy se já existirem
--   linhas colidentes na nova chave. ANTES de aplicar esta migration, rode o
--   diagnóstico abaixo e remedie eventuais colisões (ver PLANO DE REMEDIAÇÃO).
--
--   -- PRÉ-FLIGHT (rodar manualmente ANTES do deploy; NÃO faz parte do DDL):
--   SELECT company_id,
--          coalesce(agent_id, '00000000-0000-0000-0000-000000000000'::uuid) AS agent_key,
--          session_id,
--          count(*) AS n
--     FROM public.conversations
--    GROUP BY 1, 2, 3
--   HAVING count(*) > 1
--    ORDER BY n DESC;
--
--   Resultado esperado em base saudável: ZERO linhas. Enquanto a unicidade
--   GLOBAL (`conversations_session_id_key`) existir, NÃO pode haver duplicata de
--   session_id sequer global, então a nova chave (mais permissiva) também não
--   colide — o pré-flight deve retornar vazio. Linhas só apareceriam se a
--   constraint global já tivesse sido removida ANTES da hora (violação da ordem).
--
--   PLANO DE REMEDIAÇÃO (se o pré-flight retornar linhas):
--     1) Para cada grupo colidente, decidir a conversa CANÔNICA (a mais recente
--        por last_message_at / created_at) e as duplicatas a consolidar.
--     2) Re-apontar mensagens/eventos/atendimentos das duplicatas para a canônica
--        (UPDATE ... SET conversation_id = <canônica> WHERE conversation_id IN ...).
--     3) Remover as conversas duplicadas (DELETE) OU re-gerar session_id único
--        para as não-canônicas, conforme política do produto.
--     4) Reexecutar o pré-flight (deve voltar vazio) e só então aplicar esta
--        migration. NÃO aplicar com colisões pendentes — o índice falharia e o
--        deploy abortaria de forma segura.
-- ============================================================================

-- (1) Cria a unicidade MULTI-TENANT por (company_id, agent_id|sentinela, session_id)
--     ANTES de dropar a constraint global (sem janela sem unicidade).
--     coalesce(agent_id, sentinela) garante unicidade mesmo quando agent_id é NULL
--     (conversas sem agente vinculado) sem permitir colapso entre tenants.
CREATE UNIQUE INDEX IF NOT EXISTS uq_conversations_company_agent_session
  ON public.conversations(
    company_id,
    coalesce(agent_id, '00000000-0000-0000-0000-000000000000'::uuid),
    session_id
  );

-- (2) Só APÓS o novo índice unique existir e estar válido, remove a unicidade
--     GLOBAL legada de session_id (se existir). Statement separado, autocommit.
ALTER TABLE public.conversations
  DROP CONSTRAINT IF EXISTS conversations_session_id_key;
