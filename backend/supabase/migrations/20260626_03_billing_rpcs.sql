-- FASE 0B / Sprint S2 — RPCs atômicas de billing.
--
-- DEPENDE de: 20260626_01 (idempotency_key em token_usage_logs + credit_transactions)
--             20260626_02 (token_usage_outbox).
--
-- bill_usage_group (spec §3.3.3): substitui o claim-then-debit-then-compensate de
--   billing_tasks (que perdia cobrança em crash entre claim e débito, e podia dobrar
--   débito de grupos divergentes). A CLAIM-POR-LOG dentro da RPC é o gate de idempotência:
--   só linhas billed=false são reivindicadas e cobradas; o amount é recomputado SOMENTE
--   das reivindicadas. dollar_rate vem do worker (plpgsql não lê env); sell_multiplier de
--   llm_pricing. Tudo numa transação → sem janela de crash, sem dobra.
--
-- process_token_usage_outbox (spec §3.3.2): claim FOR UPDATE SKIP LOCKED + upsert
--   idempotente em token_usage_logs + delete, por linha, na MESMA transação (sem race
--   cliente). Erro permanente vira dead_at após max_attempts (sem loop infinito).
--
-- Segurança: SECURITY DEFINER, SET search_path=public, REVOKE anon/authenticated/PUBLIC,
--   GRANT EXECUTE só a service_role (espelha debit_company_balance / 20260624_atomic_balance_rpcs).
--
-- ⚠️ CUTOVER ATÔMICO (BLOCKER-2): bill_usage_group NÃO tem caller Python até o S3. NÃO aplicar
--   esta migration sozinha à frente do código. Subir, no MESMO deploy: (1) este SQL e (2) o rewrite
--   de billing_tasks (process_unbilled_usage E process_company_billing) chamando SÓ bill_usage_group
--   (sem o claim+debit-em-Python antigo). O beat process_unbilled_usage CONTINUA agendado, mas agora
--   aponta para a task REESCRITA que só passa pela RPC idempotente — então beat + drainer + on-demand
--   concorrentes NUNCA dobram (a claim-por-log `billed IS NOT TRUE` é o gate). O perigo de dobra só
--   existiria se o código ANTIGO (claim+debit em Python) coexistisse com a RPC nova; como a task foi
--   reescrita in-place, não há caminho duplo. (Cutover satisfeito por reescrever, não por remover.)
-- Rollback (combinado da FASE 0B): backend/supabase/rollbacks/20260626_billing_fase0b_rollback.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- bill_usage_group: débito atômico de um grupo (company, agent, model).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.bill_usage_group(
    p_log_ids uuid[],
    p_company_id uuid,
    p_agent_id uuid,
    p_model_name text,
    p_dollar_rate numeric
) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = public
    AS $$
DECLARE
    v_ids uuid[];
    v_cost_usd numeric;
    v_in int;
    v_out int;
    v_mult numeric;
    v_amount numeric;
    v_bal numeric;
    v_idem text;
    v_txn_id uuid;
BEGIN
    -- (1) CLAIM-POR-LOG = gate de idempotência: flipa →true só das linhas ainda não
    --     cobradas e agrega custo/tokens DELAS (CTE single-statement, RETURNING→array).
    --     `billed IS NOT TRUE` (não `= false`): lógica de 3 valores — inclui linhas
    --     legadas com billed=NULL (que `= false` ignoraria → cobrança PERDIDA).
    WITH claimed AS (
        UPDATE public.token_usage_logs
           SET billed = true, billed_at = now()
         WHERE id = ANY(p_log_ids) AND billed IS NOT TRUE
        RETURNING id, total_cost_usd, input_tokens, output_tokens
    )
    SELECT array_agg(id ORDER BY id),
           COALESCE(sum(total_cost_usd), 0),
           COALESCE(sum(input_tokens), 0),
           COALESCE(sum(output_tokens), 0)
      INTO v_ids, v_cost_usd, v_in, v_out
      FROM claimed;

    IF v_ids IS NULL THEN
        RETURN;  -- nada novo reivindicado (já cobrado / outro worker) = no-op idempotente
    END IF;

    -- (2) amount = custo_usd das REIVINDICADAS × dollar_rate (param) × sell_multiplier(modelo).
    SELECT COALESCE(sell_multiplier, 2.68) INTO v_mult
      FROM public.llm_pricing WHERE model_name = p_model_name;
    v_amount := round(v_cost_usd * p_dollar_rate * COALESCE(v_mult, 2.68), 4);

    -- Grupo sem custo (amount arredonda a 0): os logs ficam billed=true (claim do
    -- passo 1), mas NÃO geramos débito nem linha de ledger — preserva o contrato
    -- antigo ("zero-cost stays billed without a debit") e evita extrato de R$0.
    IF v_amount = 0 THEN
        RETURN;
    END IF;

    v_idem := md5(array_to_string(v_ids, ','));

    -- (3) GATE no ledger PRIMEIRO (espelha credit_company_balance / 20260624_atomic_balance_rpcs):
    --     INSERT-first idempotente em credit_transactions. Se o grupo já foi cobrado (ex.:
    --     replay após reset manual de billed), o ON CONFLICT não insere (v_txn_id NULL) e
    --     NÃO debitamos o saldo — evita débito SEM extrato (saldo ↔ ledger divergentes), que
    --     a claim sozinha (passo 1) não cobria. A claim re-flipa billed=true (correto); só o
    --     2º débito é suprimido. balance_after é preenchido no passo 5 (após o débito).
    INSERT INTO public.credit_transactions (
        company_id, agent_id, type, amount_brl, model_name,
        tokens_input, tokens_output, idempotency_key
    ) VALUES (
        p_company_id, p_agent_id, 'consumption', -v_amount, p_model_name,
        v_in, v_out, v_idem
    )
    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
    DO NOTHING
    RETURNING id INTO v_txn_id;

    IF v_txn_id IS NULL THEN
        RETURN;  -- grupo já cobrado: logs já estão billed=true (claim); sem 2º débito.
    END IF;

    -- (4) gate passou → débito atômico do saldo (guarda de linha ausente); captura balance_after.
    INSERT INTO public.company_credits (company_id, balance_brl, updated_at)
         VALUES (p_company_id, -v_amount, now())
    ON CONFLICT (company_id) DO UPDATE
         SET balance_brl = public.company_credits.balance_brl - v_amount,
             updated_at = now()
    RETURNING balance_brl INTO v_bal;

    -- (5) preenche balance_after da transação recém-criada (saldo final conhecido).
    UPDATE public.credit_transactions SET balance_after = v_bal WHERE id = v_txn_id;
END;
$$;

REVOKE ALL ON FUNCTION public.bill_usage_group(uuid[], uuid, uuid, text, numeric) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.bill_usage_group(uuid[], uuid, uuid, text, numeric) TO service_role;

-- ---------------------------------------------------------------------------
-- process_token_usage_outbox: drena o outbox idempotentemente (claim+upsert+delete).
-- Retorna o nº de linhas drenadas com sucesso.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.process_token_usage_outbox(
    p_limit int DEFAULT 100,
    p_stale_minutes int DEFAULT 5
) RETURNS int
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = public
    AS $$
DECLARE
    r RECORD;
    v_done int := 0;
BEGIN
    FOR r IN
        SELECT * FROM public.token_usage_outbox
         WHERE dead_at IS NULL
           AND (claimed_at IS NULL OR claimed_at < now() - make_interval(mins => p_stale_minutes))
         ORDER BY created_at
         LIMIT p_limit
         FOR UPDATE SKIP LOCKED
    LOOP
        BEGIN
            INSERT INTO public.token_usage_logs (
                company_id, agent_id, service_type, model_name, input_tokens, output_tokens,
                total_cost_usd, details, created_at, billed,
                cache_creation_tokens, cache_read_tokens, cached_tokens, idempotency_key
            )
            SELECT
                (r.payload->>'company_id')::uuid,
                NULLIF(r.payload->>'agent_id', '')::uuid,
                r.payload->>'service_type',
                r.payload->>'model_name',
                COALESCE((r.payload->>'input_tokens')::int, 0),
                COALESCE((r.payload->>'output_tokens')::int, 0),
                COALESCE((r.payload->>'total_cost_usd')::numeric, 0),
                COALESCE(r.payload->'details', '{}'::jsonb),
                COALESCE((r.payload->>'created_at')::timestamptz, now()),
                false,
                COALESCE((r.payload->>'cache_creation_tokens')::int, 0),
                COALESCE((r.payload->>'cache_read_tokens')::int, 0),
                COALESCE((r.payload->>'cached_tokens')::int, 0),
                r.idempotency_key
            ON CONFLICT (idempotency_key) DO NOTHING;

            DELETE FROM public.token_usage_outbox WHERE id = r.id;
            v_done := v_done + 1;
        EXCEPTION WHEN OTHERS THEN
            -- Distinguir TRANSITÓRIO de erro determinístico de DADOS (a savepoint do
            -- BEGIN/EXCEPTION reverte só este registro; o loop continua):
            IF SQLSTATE LIKE '40%'        -- 40001 serialization_failure / 40P01 deadlock_detected
               OR SQLSTATE LIKE '08%'     -- connection_exception (Broken pipe etc.)
               OR SQLSTATE IN ('55P03', '57014', '53300') THEN  -- lock_not_available / query_canceled / too_many_connections
                -- Transitório: NÃO consome tentativa nem seta claimed_at (claimed_at fica
                -- NULL → reclaim imediato no próximo tick). Só registra p/ observabilidade.
                UPDATE public.token_usage_outbox
                   SET last_error = '[transient ' || SQLSTATE || '] ' || SQLERRM
                 WHERE id = r.id;
            ELSE
                -- Erro determinístico de dados (22*/23*): incrementa attempts; dead-letter no teto.
                UPDATE public.token_usage_outbox
                   SET attempts = r.attempts + 1,
                       last_error = '[' || SQLSTATE || '] ' || SQLERRM,
                       claimed_at = now(),
                       dead_at = CASE WHEN r.attempts + 1 >= r.max_attempts THEN now() ELSE NULL END
                 WHERE id = r.id;
                IF r.attempts + 1 >= r.max_attempts THEN
                    -- DEAD-LETTER = perda definitiva de cobrança: sinal ALTO (spec §3.3 "loud").
                    -- O beat (S3) também conta dead_at IS NOT NULL e dispara Sentry/critical.
                    RAISE WARNING '[token_usage_outbox] DEAD-LETTER id=% idem=% após % tentativas (SQLSTATE %): %',
                        r.id, r.idempotency_key, r.attempts + 1, SQLSTATE, SQLERRM;
                END IF;
            END IF;
        END;
    END LOOP;

    RETURN v_done;
END;
$$;

REVOKE ALL ON FUNCTION public.process_token_usage_outbox(int, int) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.process_token_usage_outbox(int, int) TO service_role;

COMMIT;
