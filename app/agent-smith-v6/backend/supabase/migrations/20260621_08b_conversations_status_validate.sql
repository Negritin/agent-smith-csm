-- ============================================================================
-- Sprint S1 — Atendimento/SLA/Handoff — conversations_status_validate
-- SPEC-atendimento-sla-handoff-full.md §7.1 + §24 (linha 517 item b).
--
-- PASSO SEPARADO do ADD NOT VALID (20260621_08_*). Este arquivo SÓ valida as
-- constraints já adicionadas como NOT VALID, em transação distinta do ADD, para
-- que o scan de validação não seja absorvido na mesma transação/lock do ADD.
--
-- Ordenação lexicográfica: este arquivo é o `_08b_`, roda DEPOIS do `_08_`
-- (ADD NOT VALID + pré-flight gate) e ANTES do `_90_` (índices CONCURRENTLY).
--
-- IDEMPOTENTE e RE-EXECUTÁVEL com segurança:
--   * Cada VALIDATE está guardado por um DO-block que só executa se a constraint
--     EXISTIR e ainda estiver NÃO-validada (pg_constraint.convalidated = false).
--   * Se a constraint não existir (ex.: bloco DO do _08_ pulado, ou constraint
--     dropada manualmente entre execuções), o VALIDATE é PULADO (não aborta o
--     deploy com "constraint does not exist").
--   * Se já estiver validada, é no-op.
--
-- O VALIDATE escaneia a tabela sob SHARE UPDATE EXCLUSIVE (não bloqueia
-- escritas). O pré-flight gate executável do _08_ já garante que não há valores
-- fora do enum, então este VALIDATE não deve falhar por dados.
-- ============================================================================

-- conversations_status_check
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'conversations_status_check'
      AND conrelid = 'public.conversations'::regclass
      AND convalidated = false
  ) THEN
    ALTER TABLE public.conversations VALIDATE CONSTRAINT conversations_status_check;
  END IF;
END
$$;

-- conversations_sla_priority_check
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'conversations_sla_priority_check'
      AND conrelid = 'public.conversations'::regclass
      AND convalidated = false
  ) THEN
    ALTER TABLE public.conversations VALIDATE CONSTRAINT conversations_sla_priority_check;
  END IF;
END
$$;
