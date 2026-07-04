-- Testes comportamentais das RPCs de billing (pós-correções da review de 7 lentes).
\set ON_ERROR_STOP on

\set CO  '''11111111-1111-1111-1111-111111111111'''
\set AG  '''22222222-2222-2222-2222-222222222222'''

-- =====================================================================
-- BLOCKER-1: prova de que `REVOKE ... FROM anon, authenticated` (sem PUBLIC)
-- NÃO fecha o buraco, e que `FROM PUBLIC` fecha. + estado real de debit_company_balance.
-- =====================================================================
DO $$
BEGIN
    -- função throwaway: nasce com GRANT EXECUTE implícito a PUBLIC.
    CREATE FUNCTION public._priv_probe(int) RETURNS int LANGUAGE sql AS 'SELECT 1';

    IF NOT has_function_privilege('anon', 'public._priv_probe(int)', 'EXECUTE') THEN
        RAISE EXCEPTION 'pré-condição falhou: anon deveria executar via PUBLIC';
    END IF;

    -- O "fix" ERRADO (só anon/authenticated):
    REVOKE ALL ON FUNCTION public._priv_probe(int) FROM anon, authenticated;
    IF NOT has_function_privilege('anon', 'public._priv_probe(int)', 'EXECUTE') THEN
        RAISE EXCEPTION 'inesperado: revoke só de anon já teria fechado (não deveria)';
    END IF;
    RAISE NOTICE 'BLOCKER-1 confirmado: revoke só de anon/authenticated NÃO fecha (anon ainda executa via PUBLIC)';

    -- O fix CERTO:
    REVOKE ALL ON FUNCTION public._priv_probe(int) FROM PUBLIC;
    IF has_function_privilege('anon', 'public._priv_probe(int)', 'EXECUTE') THEN
        RAISE EXCEPTION 'fix FROM PUBLIC não funcionou';
    END IF;
    DROP FUNCTION public._priv_probe(int);

    -- Estado real pós-migration 04 (que agora usa FROM PUBLIC, anon, authenticated):
    IF has_function_privilege('anon', 'public.debit_company_balance(uuid,numeric)', 'EXECUTE')
       OR has_function_privilege('authenticated', 'public.debit_company_balance(uuid,numeric)', 'EXECUTE') THEN
        RAISE EXCEPTION 'BLOCKER-1 NÃO corrigido: anon/authenticated ainda executam debit_company_balance';
    END IF;
    -- E as RPCs novas também restritas:
    IF has_function_privilege('anon', 'public.bill_usage_group(uuid[],uuid,uuid,text,numeric)', 'EXECUTE')
       OR has_function_privilege('anon', 'public.process_token_usage_outbox(int,int)', 'EXECUTE') THEN
        RAISE EXCEPTION 'RPCs novas executáveis por anon (deveriam ser só service_role)';
    END IF;
    -- service_role MANTÉM:
    IF NOT has_function_privilege('service_role', 'public.bill_usage_group(uuid[],uuid,uuid,text,numeric)', 'EXECUTE') THEN
        RAISE EXCEPTION 'service_role perdeu execução de bill_usage_group';
    END IF;

    RAISE NOTICE 'BLOCKER-1 OK: debit/bill/outbox fechados p/ anon+authenticated, service_role mantém';
END $$;

-- =====================================================================
-- T1 — bill_usage_group NÃO dobra (mesmo grupo 2×)
-- =====================================================================
DO $$
DECLARE v_bal numeric; v_tx int; v_billed int;
BEGIN
    TRUNCATE public.token_usage_logs, public.credit_transactions;
    UPDATE public.company_credits SET balance_brl = 1000 WHERE company_id = '11111111-1111-1111-1111-111111111111';
    INSERT INTO public.token_usage_logs(id, company_id, agent_id, service_type, model_name, total_cost_usd, billed)
      VALUES ('aaaa0001-0000-0000-0000-000000000000','11111111-1111-1111-1111-111111111111','22222222-2222-2222-2222-222222222222','chat','gpt-test',0.0010,false),
             ('aaaa0002-0000-0000-0000-000000000000','11111111-1111-1111-1111-111111111111','22222222-2222-2222-2222-222222222222','chat','gpt-test',0.0010,false);

    PERFORM public.bill_usage_group(
        ARRAY['aaaa0001-0000-0000-0000-000000000000','aaaa0002-0000-0000-0000-000000000000']::uuid[],
        '11111111-1111-1111-1111-111111111111','22222222-2222-2222-2222-222222222222','gpt-test', 5.0);
    -- 2ª vez (idêntica): no-op
    PERFORM public.bill_usage_group(
        ARRAY['aaaa0001-0000-0000-0000-000000000000','aaaa0002-0000-0000-0000-000000000000']::uuid[],
        '11111111-1111-1111-1111-111111111111','22222222-2222-2222-2222-222222222222','gpt-test', 5.0);

    SELECT balance_brl INTO v_bal FROM public.company_credits WHERE company_id='11111111-1111-1111-1111-111111111111';
    SELECT count(*) INTO v_tx FROM public.credit_transactions;
    SELECT count(*) INTO v_billed FROM public.token_usage_logs WHERE billed = true;
    -- esperado: 1000 - (0.002 * 5 * 2) = 1000 - 0.02 = 999.98 ; 1 tx ; 2 billed
    IF v_bal <> 999.98 OR v_tx <> 1 OR v_billed <> 2 THEN
        RAISE EXCEPTION 'T1 FALHOU: balance=% (esp 999.98) tx=% (esp 1) billed=% (esp 2)', v_bal, v_tx, v_billed;
    END IF;
    -- balance_after do ledger deve estar preenchido
    IF EXISTS (SELECT 1 FROM public.credit_transactions WHERE balance_after IS NULL) THEN
        RAISE EXCEPTION 'T1 FALHOU: balance_after NULL no ledger';
    END IF;
    RAISE NOTICE 'T1 OK — sem dobra (balance %, 1 tx, 2 billed, balance_after preenchido)', v_bal;
END $$;

-- =====================================================================
-- T2 — grupos divergentes {4,5} vs {4,5,6} cobram cada log UMA vez
-- =====================================================================
DO $$
DECLARE v_bal numeric; v_tx int;
BEGIN
    TRUNCATE public.token_usage_logs, public.credit_transactions;
    UPDATE public.company_credits SET balance_brl = 1000 WHERE company_id='11111111-1111-1111-1111-111111111111';
    INSERT INTO public.token_usage_logs(id, company_id, agent_id, service_type, model_name, total_cost_usd, billed)
      VALUES ('bbbb0004-0000-0000-0000-000000000000','11111111-1111-1111-1111-111111111111','22222222-2222-2222-2222-222222222222','chat','gpt-test',0.0010,false),
             ('bbbb0005-0000-0000-0000-000000000000','11111111-1111-1111-1111-111111111111','22222222-2222-2222-2222-222222222222','chat','gpt-test',0.0010,false),
             ('bbbb0006-0000-0000-0000-000000000000','11111111-1111-1111-1111-111111111111','22222222-2222-2222-2222-222222222222','chat','gpt-test',0.0010,false);

    PERFORM public.bill_usage_group(
        ARRAY['bbbb0004-0000-0000-0000-000000000000','bbbb0005-0000-0000-0000-000000000000']::uuid[],
        '11111111-1111-1111-1111-111111111111','22222222-2222-2222-2222-222222222222','gpt-test', 5.0);
    PERFORM public.bill_usage_group(
        ARRAY['bbbb0004-0000-0000-0000-000000000000','bbbb0005-0000-0000-0000-000000000000','bbbb0006-0000-0000-0000-000000000000']::uuid[],
        '11111111-1111-1111-1111-111111111111','22222222-2222-2222-2222-222222222222','gpt-test', 5.0);

    SELECT balance_brl INTO v_bal FROM public.company_credits WHERE company_id='11111111-1111-1111-1111-111111111111';
    SELECT count(*) INTO v_tx FROM public.credit_transactions;
    -- esperado: cobra 4,5 (0.02) + 6 (0.01) = 0.03 → 999.97 ; 2 tx
    IF v_bal <> 999.97 OR v_tx <> 2 THEN
        RAISE EXCEPTION 'T2 FALHOU: balance=% (esp 999.97) tx=% (esp 2)', v_bal, v_tx;
    END IF;
    RAISE NOTICE 'T2 OK — grupos divergentes não dobram (balance %, 2 tx)', v_bal;
END $$;

-- =====================================================================
-- T5 — INSERT-GATE-FIRST: replay APÓS reset manual de billed NÃO dobra débito (HIGH-4)
-- =====================================================================
DO $$
DECLARE v_bal numeric; v_tx int;
BEGIN
    TRUNCATE public.token_usage_logs, public.credit_transactions;
    UPDATE public.company_credits SET balance_brl = 1000 WHERE company_id='11111111-1111-1111-1111-111111111111';
    INSERT INTO public.token_usage_logs(id, company_id, agent_id, service_type, model_name, total_cost_usd, billed)
      VALUES ('cccc0001-0000-0000-0000-000000000000','11111111-1111-1111-1111-111111111111','22222222-2222-2222-2222-222222222222','chat','gpt-test',0.0010,false),
             ('cccc0002-0000-0000-0000-000000000000','11111111-1111-1111-1111-111111111111','22222222-2222-2222-2222-222222222222','chat','gpt-test',0.0010,false);

    PERFORM public.bill_usage_group(
        ARRAY['cccc0001-0000-0000-0000-000000000000','cccc0002-0000-0000-0000-000000000000']::uuid[],
        '11111111-1111-1111-1111-111111111111','22222222-2222-2222-2222-222222222222','gpt-test', 5.0);  -- → 999.98

    -- SIMULA reset manual (compensação/erro op): billed volta a false.
    UPDATE public.token_usage_logs SET billed = false
     WHERE id IN ('cccc0001-0000-0000-0000-000000000000','cccc0002-0000-0000-0000-000000000000');

    -- Re-cobrança do MESMO grupo: a claim re-flipa billed=true, MAS o gate (idem igual)
    -- bloqueia o 2º débito. Sem o insert-gate-first, o saldo cairia p/ 999.96 (dobra) e
    -- ficaria SEM extrato (divergência saldo↔ledger).
    PERFORM public.bill_usage_group(
        ARRAY['cccc0001-0000-0000-0000-000000000000','cccc0002-0000-0000-0000-000000000000']::uuid[],
        '11111111-1111-1111-1111-111111111111','22222222-2222-2222-2222-222222222222','gpt-test', 5.0);

    SELECT balance_brl INTO v_bal FROM public.company_credits WHERE company_id='11111111-1111-1111-1111-111111111111';
    SELECT count(*) INTO v_tx FROM public.credit_transactions;
    IF v_bal <> 999.98 OR v_tx <> 1 THEN
        RAISE EXCEPTION 'T5 FALHOU (dobra pós-reset): balance=% (esp 999.98) tx=% (esp 1)', v_bal, v_tx;
    END IF;
    -- consistência saldo↔ledger: o único extrato registra -0.02
    IF (SELECT amount_brl FROM public.credit_transactions LIMIT 1) <> -0.0200 THEN
        RAISE EXCEPTION 'T5 FALHOU: extrato divergente';
    END IF;
    RAISE NOTICE 'T5 OK — insert-gate-first impede dobra pós-reset (balance %, 1 tx, saldo↔ledger consistentes)', v_bal;
END $$;

-- =====================================================================
-- T3 — process_token_usage_outbox idempotente (mesmo idem 2×)
-- =====================================================================
DO $$
DECLARE v_logs int; v_outbox int; k uuid := 'dddd0001-0000-0000-0000-000000000000';
BEGIN
    TRUNCATE public.token_usage_logs, public.token_usage_outbox;
    INSERT INTO public.token_usage_outbox(idempotency_key, company_id, payload)
      VALUES (k, '11111111-1111-1111-1111-111111111111', jsonb_build_object(
        'company_id','11111111-1111-1111-1111-111111111111','service_type','chat','model_name','gpt-test',
        'input_tokens',10,'output_tokens',20,'total_cost_usd',0.001));
    PERFORM public.process_token_usage_outbox(100, 0);

    -- re-enfileira a MESMA idem (replay)
    INSERT INTO public.token_usage_outbox(idempotency_key, company_id, payload)
      VALUES (k, '11111111-1111-1111-1111-111111111111', jsonb_build_object(
        'company_id','11111111-1111-1111-1111-111111111111','service_type','chat','model_name','gpt-test',
        'input_tokens',10,'output_tokens',20,'total_cost_usd',0.001));
    PERFORM public.process_token_usage_outbox(100, 0);

    SELECT count(*) INTO v_logs FROM public.token_usage_logs WHERE idempotency_key = k;
    SELECT count(*) INTO v_outbox FROM public.token_usage_outbox;
    IF v_logs <> 1 OR v_outbox <> 0 THEN
        RAISE EXCEPTION 'T3 FALHOU: logs=% (esp 1) outbox=% (esp 0)', v_logs, v_outbox;
    END IF;
    RAISE NOTICE 'T3 OK — outbox idempotente (1 log, 0 outbox)';
END $$;

-- =====================================================================
-- T4 — payload inválido (data error) vira dead-letter (sem perda, sem loop)
-- =====================================================================
DO $$
DECLARE v_dead timestamptz; v_attempts int; v_logs int;
BEGIN
    TRUNCATE public.token_usage_logs, public.token_usage_outbox;
    -- company_id inválido → 22P02 ao castar (erro DETERMINÍSTICO de dados). max_attempts=1.
    INSERT INTO public.token_usage_outbox(idempotency_key, company_id, payload, max_attempts)
      VALUES ('eeee0001-0000-0000-0000-000000000000','11111111-1111-1111-1111-111111111111', jsonb_build_object(
        'company_id','NOT-A-UUID','service_type','chat','model_name','gpt-test'), 1);
    PERFORM public.process_token_usage_outbox(100, 0);

    SELECT dead_at, attempts INTO v_dead, v_attempts FROM public.token_usage_outbox LIMIT 1;
    SELECT count(*) INTO v_logs FROM public.token_usage_logs;
    IF v_dead IS NULL OR v_attempts <> 1 OR v_logs <> 0 THEN
        RAISE EXCEPTION 'T4 FALHOU: dead_at=% attempts=% (esp 1) logs=% (esp 0)', v_dead, v_attempts, v_logs;
    END IF;
    RAISE NOTICE 'T4 OK — payload inválido vira dead-letter (dead_at set, attempts=1, 0 logs)';
END $$;

-- =====================================================================
-- T6 — erro TRANSITÓRIO (40001) NÃO consome tentativa nem dead-letra (MEDIUM-7)
-- =====================================================================
DO $$
DECLARE v_dead timestamptz; v_attempts int; v_err text; v_outbox int;
BEGIN
    TRUNCATE public.token_usage_logs, public.token_usage_outbox;
    -- trigger que injeta um erro transitório (serialization_failure) só p/ o marcador.
    CREATE OR REPLACE FUNCTION public._inject_transient() RETURNS trigger LANGUAGE plpgsql AS $f$
    BEGIN
        IF NEW.service_type = 'TRANSIENT_MARKER' THEN
            RAISE EXCEPTION 'simulated serialization failure' USING ERRCODE = '40001';
        END IF;
        RETURN NEW;
    END $f$;
    CREATE TRIGGER _trg_transient BEFORE INSERT ON public.token_usage_logs
        FOR EACH ROW EXECUTE FUNCTION public._inject_transient();

    INSERT INTO public.token_usage_outbox(idempotency_key, company_id, payload, max_attempts)
      VALUES ('ffff0001-0000-0000-0000-000000000000','11111111-1111-1111-1111-111111111111', jsonb_build_object(
        'company_id','11111111-1111-1111-1111-111111111111','service_type','TRANSIENT_MARKER','model_name','gpt-test'), 10);
    PERFORM public.process_token_usage_outbox(100, 0);

    SELECT dead_at, attempts, last_error INTO v_dead, v_attempts, v_err FROM public.token_usage_outbox LIMIT 1;
    SELECT count(*) INTO v_outbox FROM public.token_usage_outbox;
    -- esperado: attempts NÃO incrementa (0), NÃO dead, fica no outbox p/ retry, last_error marca transient
    IF v_dead IS NOT NULL OR v_attempts <> 0 OR v_outbox <> 1 OR v_err NOT LIKE '%transient%' THEN
        RAISE EXCEPTION 'T6 FALHOU: dead_at=% attempts=% (esp 0) outbox=% (esp 1) err=%', v_dead, v_attempts, v_outbox, v_err;
    END IF;
    DROP TRIGGER _trg_transient ON public.token_usage_logs;
    DROP FUNCTION public._inject_transient();
    RAISE NOTICE 'T6 OK — transitório (40001) não consome tentativa nem dead-letra (attempts=0, fica p/ retry)';
END $$;

-- =====================================================================
-- T7 — grupo ZERO-CUSTO fica billed sem débito nem ledger (contrato preservado)
-- =====================================================================
DO $$
DECLARE v_bal numeric; v_tx int; v_billed int;
BEGIN
    TRUNCATE public.token_usage_logs, public.credit_transactions;
    UPDATE public.company_credits SET balance_brl = 1000 WHERE company_id='11111111-1111-1111-1111-111111111111';
    INSERT INTO public.token_usage_logs(id, company_id, agent_id, service_type, model_name, total_cost_usd, billed)
      VALUES ('77770001-0000-0000-0000-000000000000','11111111-1111-1111-1111-111111111111','22222222-2222-2222-2222-222222222222','chat','gpt-test',0,false);

    PERFORM public.bill_usage_group(
        ARRAY['77770001-0000-0000-0000-000000000000']::uuid[],
        '11111111-1111-1111-1111-111111111111','22222222-2222-2222-2222-222222222222','gpt-test', 5.0);

    SELECT balance_brl INTO v_bal FROM public.company_credits WHERE company_id='11111111-1111-1111-1111-111111111111';
    SELECT count(*) INTO v_tx FROM public.credit_transactions;
    SELECT count(*) INTO v_billed FROM public.token_usage_logs WHERE billed = true;
    -- esperado: saldo intacto (1000), 0 ledger, log fica billed=true (claim)
    IF v_bal <> 1000 OR v_tx <> 0 OR v_billed <> 1 THEN
        RAISE EXCEPTION 'T7 FALHOU: balance=% (esp 1000) tx=% (esp 0) billed=% (esp 1)', v_bal, v_tx, v_billed;
    END IF;
    RAISE NOTICE 'T7 OK — grupo zero-custo billed sem débito/ledger (balance 1000, 0 tx, 1 billed)';
END $$;

-- =====================================================================
-- T8 — log legado com billed=NULL é REIVINDICADO e cobrado (gate 3-valued safe)
-- =====================================================================
DO $$
DECLARE v_bal numeric; v_tx int; v_billed int;
BEGIN
    TRUNCATE public.token_usage_logs, public.credit_transactions;
    UPDATE public.company_credits SET balance_brl = 1000 WHERE company_id='11111111-1111-1111-1111-111111111111';
    -- billed = NULL (linha legada). `billed = false` ignoraria; `billed IS NOT TRUE` claima.
    INSERT INTO public.token_usage_logs(id, company_id, agent_id, service_type, model_name, total_cost_usd, billed)
      VALUES ('88880001-0000-0000-0000-000000000000','11111111-1111-1111-1111-111111111111','22222222-2222-2222-2222-222222222222','chat','gpt-test',0.0010, NULL);

    PERFORM public.bill_usage_group(
        ARRAY['88880001-0000-0000-0000-000000000000']::uuid[],
        '11111111-1111-1111-1111-111111111111','22222222-2222-2222-2222-222222222222','gpt-test', 5.0);

    SELECT balance_brl INTO v_bal FROM public.company_credits WHERE company_id='11111111-1111-1111-1111-111111111111';
    SELECT count(*) INTO v_tx FROM public.credit_transactions;
    SELECT count(*) INTO v_billed FROM public.token_usage_logs WHERE billed = true;
    -- esperado: cobrou (0.001*5*2=0.01 → 999.99), 1 ledger, billed=true (não perdeu)
    IF v_bal <> 999.99 OR v_tx <> 1 OR v_billed <> 1 THEN
        RAISE EXCEPTION 'T8 FALHOU (billed=NULL perdido): balance=% (esp 999.99) tx=% (esp 1) billed=% (esp 1)', v_bal, v_tx, v_billed;
    END IF;
    RAISE NOTICE 'T8 OK — billed=NULL legado é reivindicado e cobrado (balance 999.99, 1 tx, 1 billed)';
END $$;

SELECT '==== TODOS OS TESTES PASSARAM ====' AS resultado;
