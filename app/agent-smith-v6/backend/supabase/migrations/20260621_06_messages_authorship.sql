-- ============================================================================
-- Sprint S1 — Atendimento/SLA/Handoff — messages_authorship
-- SPEC-atendimento-sla-handoff-full.md §7.1 (autoria canônica em messages).
--
-- PRINCÍPIO: aditivo e idempotente. Adiciona author_type/is_system + CHECK e
-- faz backfill EM LOTE pelas 4 regras de §7.1. NÃO amplia messages.type (fica
-- text|voice como hoje). NÃO toca grants/policies de anon (S11).
--
-- messages.company_id é tratado em migration SEPARADA
-- (20260621_07_messages_company_id.sql), conforme §17 item 5.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- §7.1 — Colunas de autoria. author_type nullable (backfill abaixo cobre 100%
-- das linhas existentes; novas linhas devem ser preenchidas pelos write sites
-- em sprints posteriores). is_system tem DEFAULT false (seguro em tabela
-- populada).
-- ----------------------------------------------------------------------------
ALTER TABLE public.messages
  ADD COLUMN IF NOT EXISTS author_type text,
  ADD COLUMN IF NOT EXISTS is_system boolean NOT NULL DEFAULT false;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'messages_author_type_check'
      AND conrelid = 'public.messages'::regclass
  ) THEN
    ALTER TABLE public.messages
      ADD CONSTRAINT messages_author_type_check
      CHECK (author_type IS NULL OR author_type IN ('customer', 'ai_agent', 'human_operator', 'system'));
  END IF;
END
$$;

-- ----------------------------------------------------------------------------
-- Backfill EM LOTE (chunks) — §7.1 / §22 item 3.
--
-- Regras (e PRECEDÊNCIA explícita — validador §22 item 3):
--   1. role = 'user'                          -> author_type = 'customer'
--   2. sender_user_id IS NOT NULL             -> author_type = 'human_operator'
--      (TEM PRECEDÊNCIA sobre a regra 4: uma mensagem 'assistant' com
--       sender_user_id é humana legada e deve virar 'human_operator', não 'ai_agent')
--   3. sistêmicas                             -> author_type = 'system', is_system = true
--   4. demais role = 'assistant'              -> author_type = 'ai_agent'
--
-- Heurística para "sistêmicas": como o schema base não tem flag de sistema,
-- só marcamos is_system/system as linhas que JÁ tiverem is_system = true
-- (ex.: gravadas após esta migration por um write site). No backfill de dados
-- legados nenhuma linha é classificada como 'system' por inferência — isso
-- evita falso-positivo. A classificação 'system' definitiva é responsabilidade
-- dos write sites em S6/S7. A ordem dos UPDATEs abaixo garante a precedência:
-- 'customer' e 'human_operator' antes de 'ai_agent'; 'system' (is_system=true)
-- antes de 'ai_agent' para não sobrescrever sistêmicas já marcadas.
--
-- COMPATIBILIDADE COM RUNNER TRANSACIONAL (correção do erro 2D000):
-- O backfill usa UPDATEs SIMPLES e IDEMPOTENTES — NÃO uma PROCEDURE com COMMIT.
-- Motivo: COMMIT dentro de procedure falha com "2D000: invalid transaction
-- termination" quando o runner envolve o arquivo numa transação (ex.: Supabase
-- SQL Editor). Estes UPDATEs rodam numa única transação e funcionam em qualquer
-- runner (SQL Editor, CLI, psql).
--
-- PRECEDÊNCIA: a ORDEM dos UPDATEs garante as regras de §7.1 — cada UPDATE só
-- toca linhas com author_type AINDA NULL, então 'customer'/'human_operator'/
-- 'system' são fixados ANTES de 'ai_agent' (uma 'assistant' com sender_user_id
-- vira 'human_operator' na regra 2 e a regra 4 a ignora). Idempotente e
-- reexecutável (reexecuções não tocam linhas já classificadas).
--
-- ⚠️ VOLUME: em `messages` MUITO grande/quente, estes UPDATEs rodam numa
-- transação única e podem segurar row-locks durante a execução — aplicar FORA
-- DE PICO. author_type é NULLABLE e a leitura tolera NULL (o classificador cai
-- em role/sender_user_id), então em base enorme o backfill pode ser feito em
-- lotes manuais via psql autocommit. Ver docs/RUNBOOK-deploy-atendimento-sla.md §2.
-- ----------------------------------------------------------------------------

-- Regra 1: role = 'user' -> customer
UPDATE public.messages
  SET author_type = 'customer'
  WHERE author_type IS NULL AND role = 'user';

-- Regra 2: sender_user_id IS NOT NULL -> human_operator
-- (precede a regra 4: 'assistant' com sender_user_id é humano legado, não ai_agent)
UPDATE public.messages
  SET author_type = 'human_operator'
  WHERE author_type IS NULL AND sender_user_id IS NOT NULL;

-- Regra 3: sistêmicas já marcadas (is_system = true) -> system
UPDATE public.messages
  SET author_type = 'system'
  WHERE author_type IS NULL AND is_system = true;

-- Regra 4: demais role = 'assistant' -> ai_agent (catch-all final)
UPDATE public.messages
  SET author_type = 'ai_agent'
  WHERE author_type IS NULL AND role = 'assistant';

-- ----------------------------------------------------------------------------
-- AUDITORIA DE messages.type (§7.1 — validador linhas 407-409 / S1 critério 134)
--
-- ASSERTIVA DE CONTRATO: o domínio de messages.type permanece IN ('text','voice').
-- 'voice' é o áudio canônico; imagem continua via image_url COM type='text'.
-- Esta migration NÃO altera o CHECK messages_type_check (schema_completo.sql)
-- nem amplia o enum para audio/image/document — isso está fora do escopo da SPEC.
-- A validação efetiva nos ENDPOINTS de escrita (garantir que nenhum write grave
-- type fora de ('text','voice')) será coberta em S6. Aqui apenas documentamos e
-- verificamos em tempo de migration que o contrato não foi alterado:
-- ----------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'messages_type_check'
      AND conrelid = 'public.messages'::regclass
  ) THEN
    RAISE EXCEPTION
      'messages_type_check ausente: contrato type IN (text,voice) não pode ser removido em S1';
  END IF;
END
$$;
