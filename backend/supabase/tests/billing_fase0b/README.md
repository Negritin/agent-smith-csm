# Validação comportamental — Billing FASE 0B (Sprint S2)

Harness que valida **empiricamente** (Postgres real, não mock) as migrations de billing
da FASE 0B contra os modos de falha que "não podem quebrar": **dobra de cobrança**,
**perda de cobrança**, idempotência e segurança de privilégio.

Migrations sob teste (`backend/supabase/migrations/`):
- `20260626_01_billing_idempotency_keys.sql`
- `20260626_02_token_usage_outbox.sql`
- `20260626_03_billing_rpcs.sql` (`bill_usage_group`, `process_token_usage_outbox`)
- `20260626_04_revoke_debit_company_balance.sql`

## Como rodar

Requer só os binários do Postgres (≥14); não precisa de Supabase CLI, Docker nem sudo.
Sobe um cluster descartável e aplica schema → migrations → testes.

```bash
PGBIN=/usr/lib/postgresql/18/bin            # ajuste p/ sua versão
export PGDATA=$(mktemp -d) PGHOST=/tmp PGPORT=5433 PGUSER=postgres
"$PGBIN/initdb" -U postgres -A trust >/dev/null
"$PGBIN/pg_ctl" -D "$PGDATA" -o "-p 5433 -k /tmp" -l "$PGDATA/log" start
sleep 1

"$PGBIN/psql" -d postgres -c "CREATE DATABASE smith_test;"
D=backend/supabase
"$PGBIN/psql" -d smith_test -v ON_ERROR_STOP=1 -f $D/tests/billing_fase0b/00_schema.sql
for m in 01_billing_idempotency_keys 02_token_usage_outbox 03_billing_rpcs 04_revoke_debit_company_balance; do
  "$PGBIN/psql" -d smith_test -v ON_ERROR_STOP=1 -f $D/migrations/20260626_$m.sql
done
"$PGBIN/psql" -d smith_test -v ON_ERROR_STOP=1 -f $D/tests/billing_fase0b/99_behavioral_tests.sql

"$PGBIN/pg_ctl" -D "$PGDATA" stop; rm -rf "$PGDATA"
```

Sucesso = última linha `==== TODOS OS TESTES PASSARAM ====`. Qualquer falha aborta
(`RAISE EXCEPTION` + `ON_ERROR_STOP`).

> `00_schema.sql` é um **subset fiel** do schema real (só as tabelas/roles que as RPCs
> tocam). Não substitui a revisão contra `schema_completo.sql`; complementa-a com prova
> comportamental.

## O que cada teste prova

| Teste | Garante |
|-------|---------|
| **BLOCKER-1** | `REVOKE ... FROM anon, authenticated` (sem `PUBLIC`) **não** fecha o privilégio (anon executa via PUBLIC); só `FROM PUBLIC` fecha. `debit_company_balance`/`bill_usage_group`/`process_token_usage_outbox` ficam restritos a `service_role`. |
| **T1** | `bill_usage_group` no MESMO grupo 2× não dobra (claim-por-log = gate). |
| **T2** | Grupos divergentes `{4,5}` vs `{4,5,6}` cobram cada log exatamente 1×. |
| **T5** | **Insert-gate-first**: re-cobrança após reset manual de `billed` não dobra o débito nem deixa saldo sem extrato (consistência saldo↔ledger). |
| **T3** | `process_token_usage_outbox` drena idempotentemente (replay da mesma `idempotency_key` → 1 log). |
| **T4** | Payload com erro determinístico de dados vira **dead-letter** (`dead_at` set), sem perda silenciosa e sem loop; emite `WARNING` alto. |
| **T6** | Erro **transitório** (`40001`) **não** consome tentativa nem dead-letra — fica no outbox p/ retry. |

Origem: correções da revisão adversarial de 7 lentes Opus (workflow `w876fpzze`).
