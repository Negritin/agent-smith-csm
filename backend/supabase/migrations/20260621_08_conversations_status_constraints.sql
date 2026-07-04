-- ============================================================================
-- Sprint S1 — Atendimento/SLA/Handoff — conversations_status_constraints
-- SPEC-atendimento-sla-handoff-full.md §7.1 + §19 Fase 1 item 5 + §24.
--
-- Adiciona conversations_status_check e conversations_sla_priority_check de forma
-- NÃO-QUEBRA:
--   (a) PRÉ-FLIGHT EXECUTÁVEL (gate) de status legados: normaliza o que conhece
--       e, se sobrar qualquer valor fora dos 7 canônicos, FALHA CEDO com a lista
--       exata (RAISE EXCEPTION) — não deixa o VALIDATE estourar a meio caminho.
--   (b) Normalização de valores fora do enum ANTES de validar.
--   (c) Constraint adicionada como NOT VALID (não bloqueia/escaneia a tabela).
--   (d) VALIDATE CONSTRAINT em ARQUIVO/transação SEPARADO (20260621_08b_*).
--
-- Os 7 status canônicos (§6.1, inclui legados 'open' e 'HUMAN_REQUESTED'):
--   open, HUMAN_REQUESTED, HUMAN_ACTIVE, PENDING_CUSTOMER, RETURNED_TO_AI,
--   RESOLVED, CLOSED.
--
-- NOTA: conversations.status é character varying(20) (schema_completo.sql).
-- O CHECK é compatível com varchar (comparação textual). Não alteramos o tipo.
--
-- SOBRE O VALIDATE (§24, linha 517 item b): o VALIDATE CONSTRAINT foi movido para
-- o arquivo SEPARADO 20260621_08b_conversations_status_validate.sql para garantir
-- que rode em transação distinta do ADD NOT VALID. Assim o scan/validação não é
-- absorvido na mesma transação do ADD (preservando o ganho do NOT VALID mesmo em
-- runners caseiros que agrupem statements de um arquivo numa única transação).
-- ============================================================================

-- ----------------------------------------------------------------------------
-- (b) NORMALIZAÇÃO de valores legados/fora do enum ANTES do VALIDATE.
-- Defensivo e idempotente: status NULL ou string vazia -> 'open' (estado
-- inicial); variações de caixa conhecidas -> forma canônica. Não toca em linhas
-- que já estão em um dos 7 valores canônicos. O WHERE restringe o UPDATE às
-- linhas realmente fora do canônico (higiene de reexecução — não varre a tabela
-- inteira em runs subsequentes).
-- ----------------------------------------------------------------------------
UPDATE public.conversations
  SET status = 'open'
  WHERE status IS NULL OR btrim(status::text) = '';

-- Status legado 'active' (criação de conversa pelo dashboard do usuário, antes de
-- S6) -> 'open' (estado inicial canônico, §6.1). Sem isto, o pré-flight abaixo
-- abortaria o deploy se já existirem linhas com 'active' no banco, e o CHECK
-- rejeitaria novos INSERTs. A rota app/api/conversations/route.ts foi corrigida
-- para inserir 'open' diretamente (S6, lente NÃO-QUEBRA).
UPDATE public.conversations
  SET status = 'open'
  WHERE lower(btrim(status::text)) = 'active';

-- Mapeia variantes de caixa para a forma canônica (defensivo; no-op se já canônico).
UPDATE public.conversations
  SET status = canonical.s
  FROM (VALUES
    ('open',             'open'),
    ('human_requested',  'HUMAN_REQUESTED'),
    ('human_active',     'HUMAN_ACTIVE'),
    ('pending_customer', 'PENDING_CUSTOMER'),
    ('returned_to_ai',   'RETURNED_TO_AI'),
    ('resolved',         'RESOLVED'),
    ('closed',           'CLOSED')
  ) AS canonical(legacy, s)
  WHERE lower(public.conversations.status::text) = canonical.legacy
    AND public.conversations.status::text <> canonical.s;

-- ----------------------------------------------------------------------------
-- (a) PRÉ-FLIGHT EXECUTÁVEL (gate). Depois da normalização acima, se AINDA
-- existir qualquer status fora dos 7 canônicos, ABORTA o deploy aqui — com a
-- lista exata dos valores ofensivos — em vez de deixar o VALIDATE (no _08b_)
-- estourar com mensagem genérica a meio caminho. Falha cedo, clara e acionável.
-- ----------------------------------------------------------------------------
DO $$
DECLARE
  invalid_values text;
BEGIN
  SELECT string_agg(quote_literal(coalesce(status::text, '<NULL>')), ', ' ORDER BY coalesce(status::text, '<NULL>'))
    INTO invalid_values
  FROM (
    SELECT DISTINCT status
    FROM public.conversations
    WHERE status IS NULL
       OR status::text NOT IN (
         'open',
         'HUMAN_REQUESTED',
         'HUMAN_ACTIVE',
         'PENDING_CUSTOMER',
         'RETURNED_TO_AI',
         'RESOLVED',
         'CLOSED'
       )
  ) AS bad;

  IF invalid_values IS NOT NULL THEN
    RAISE EXCEPTION
      'conversations.status contém valores fora dos 7 canônicos e não pôde ser normalizado automaticamente: %. Normalize esses valores manualmente (ex.: para ''open'' ou ''CLOSED'') antes de reaplicar a migration. A constraint conversations_status_check NÃO será validada enquanto existirem valores inválidos.',
      invalid_values;
  END IF;
END
$$;

-- ----------------------------------------------------------------------------
-- (c) conversations_status_check como NOT VALID (idempotente).
-- ----------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'conversations_status_check'
      AND conrelid = 'public.conversations'::regclass
  ) THEN
    ALTER TABLE public.conversations
      ADD CONSTRAINT conversations_status_check
      CHECK (status IN (
        'open',
        'HUMAN_REQUESTED',
        'HUMAN_ACTIVE',
        'PENDING_CUSTOMER',
        'RETURNED_TO_AI',
        'RESOLVED',
        'CLOSED'
      ))
      NOT VALID;
  END IF;
END
$$;

-- ----------------------------------------------------------------------------
-- (c) conversations_sla_priority_check como NOT VALID (idempotente).
-- ----------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'conversations_sla_priority_check'
      AND conrelid = 'public.conversations'::regclass
  ) THEN
    ALTER TABLE public.conversations
      ADD CONSTRAINT conversations_sla_priority_check
      CHECK (sla_priority IS NULL OR sla_priority IN ('normal', 'high', 'critical'))
      NOT VALID;
  END IF;
END
$$;

-- ----------------------------------------------------------------------------
-- (d) VALIDATE CONSTRAINT: ver arquivo SEPARADO 20260621_08b_*.
-- Mantido fora deste arquivo (transação distinta do ADD NOT VALID acima) para
-- preservar o objetivo do NOT VALID — não escanear a tabela sob o mesmo lock do
-- ADD. SPEC §7.1 / §24 (linha 517 item b): "adicionar como NOT VALID e SÓ DEPOIS
-- VALIDATE CONSTRAINT em passo separado".
-- ----------------------------------------------------------------------------
