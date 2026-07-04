-- ============================================================================
-- Forward fix (S8) — conversation_inactivity_timers: status 'processing'
--
-- PROBLEMA: 20260621_05_conversation_inactivity_timers.sql foi inicialmente
-- commitada (commit fc3b34a) SEM 'processing' no CHECK de status e com o unique
-- parcial uq_inactivity_timers_one_scheduled restrito a WHERE status='scheduled'.
-- A correção do S8 editou aquele arquivo in place, mas usa CREATE TABLE IF NOT
-- EXISTS / CREATE UNIQUE INDEX IF NOT EXISTS — portanto QUALQUER ambiente que já
-- aplicou a versão antiga do _05 NÃO ganha 'processing' no CHECK nem o predicado
-- ampliado do índice. Nesses ambientes, o claim atômico do worker
-- (inactivity_timer_service.py: UPDATE ... SET status='processing') dispara
-- 23514 (check_violation) e NENHUM timer auto-fecha — o BLOCKER que o S8 corrige.
--
-- Esta migration é forward, idempotente e segura para reaplicar:
--   1) Recria o CHECK de status incluindo 'processing'.
--   2) Recria o unique parcial cobrindo ('scheduled','processing').
-- Para instalações novas (que já aplicaram o _05 corrigido) é um no-op efetivo:
-- o estado final é idêntico ao do _05.
--
-- PRINCÍPIO: aditivo e idempotente. Sem mudança de dados.
-- ============================================================================

DO $$
DECLARE
  v_constraint_name text;
BEGIN
  -- A tabela pode não existir em ambientes que nunca aplicaram o _05 (ex.: bases
  -- montadas só a partir do schema_completo). Nesse caso, nada a fazer aqui.
  IF to_regclass('public.conversation_inactivity_timers') IS NULL THEN
    RAISE NOTICE 'conversation_inactivity_timers ausente; pulando fix de status.';
    RETURN;
  END IF;

  -- Localiza o nome (auto-gerado) do CHECK de status. O CHECK inline da
  -- CREATE TABLE recebe um nome do tipo <tabela>_status_check, mas resolvemos
  -- dinamicamente para cobrir variações.
  SELECT con.conname
    INTO v_constraint_name
    FROM pg_constraint con
    JOIN pg_class rel ON rel.oid = con.conrelid
    JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
   WHERE nsp.nspname = 'public'
     AND rel.relname = 'conversation_inactivity_timers'
     AND con.contype = 'c'
     AND pg_get_constraintdef(con.oid) ILIKE '%status%'
   LIMIT 1;

  IF v_constraint_name IS NOT NULL THEN
    EXECUTE format(
      'ALTER TABLE public.conversation_inactivity_timers DROP CONSTRAINT %I',
      v_constraint_name
    );
  END IF;

  ALTER TABLE public.conversation_inactivity_timers
    ADD CONSTRAINT conversation_inactivity_timers_status_check
    CHECK (status IN ('scheduled', 'processing', 'cancelled', 'executed', 'failed'));
END
$$;

-- Recria o unique parcial cobrindo 'processing'. Um timer em 'processing' ainda é
-- o timer ATIVO da conversa (a tick que o reivindicou está fechando-a), então o
-- unique precisa impedir que um novo 'scheduled' nasça durante o processamento.
DROP INDEX IF EXISTS public.uq_inactivity_timers_one_scheduled;

CREATE UNIQUE INDEX IF NOT EXISTS uq_inactivity_timers_one_scheduled
  ON public.conversation_inactivity_timers(conversation_id, timer_type)
  WHERE status IN ('scheduled', 'processing');
