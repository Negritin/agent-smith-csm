#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${APP_DIR:-$REPO_ROOT/app/agent-smith-v6}"
BACKEND_DIR="$APP_DIR/backend"
SUPABASE_DIR="$BACKEND_DIR/supabase"
APP_ENV_FILE="${APP_ENV_FILE:-/opt/agent-smith/.env.app}"
MODE="${1:-fresh}"

if [ -f "$APP_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$APP_ENV_FILE"
  set +a
fi

die() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

run_psql() {
  local file="$1"
  local rel="${file#$SUPABASE_DIR/}"

  printf 'psql: %s\n' "$rel"

  if command -v psql >/dev/null 2>&1; then
    psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f "$file"
    return
  fi

  docker run --rm \
    -v "$SUPABASE_DIR:/supabase:ro" \
    postgres:16-alpine \
    psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f "/supabase/$rel"
}

fresh_files=(
  "$SUPABASE_DIR/migrations/schema_completo_v7.0.sql"
  "$SUPABASE_DIR/migrations/storage_buckets.sql"
  "$SUPABASE_DIR/seed_llm_pricing.sql"
  "$SUPABASE_DIR/seed_platform_settings.sql"
)

upgrade_files=(
  "$SUPABASE_DIR/migrations/20260528_sprint4_database_security.sql"
  "$SUPABASE_DIR/migrations/20260528_sprint7_security_audit_triggers.sql"
  "$SUPABASE_DIR/migrations/20260528_widget_messages_scoped_rpc.sql"
  "$SUPABASE_DIR/migrations/20260528_widget_hmac_private_secret_hotfix.sql"
  "$SUPABASE_DIR/migrations/20260528_admin_users_master_role_hotfix.sql"
  "$SUPABASE_DIR/migrations/20260528_add_config_updated_at_to_agent_mcp_connections.sql"
  "$SUPABASE_DIR/migrations/20260528_add_config_updated_at_to_ucp_connections.sql"
  "$SUPABASE_DIR/migrations/20260528_add_updated_at_to_agent_mcp_tools.sql"
  "$SUPABASE_DIR/migrations/20260529_model_evolution.sql"
  "$SUPABASE_DIR/migrations/20260530_atomic_conversation_unread.sql"
  "$SUPABASE_DIR/migrations/20260530_platform_settings.sql"
  "$SUPABASE_DIR/migrations/20260601_add_updated_at_trigger_to_agents.sql"
  "$SUPABASE_DIR/migrations/20260601_credit_transactions_stripe_payment_unique.sql"
  "$SUPABASE_DIR/migrations/20260612_mcp_remote_servers.sql"
  "$SUPABASE_DIR/migrations/20260615_seed_mcp_remote_servers.sql"
  "$SUPABASE_DIR/migrations/20260620_uazapi_integration.sql"
  "$SUPABASE_DIR/migrations/20260621_01_attendance_core.sql"
  "$SUPABASE_DIR/migrations/20260621_02_sla_core.sql"
  "$SUPABASE_DIR/migrations/20260621_03_notifications_blocklist.sql"
  "$SUPABASE_DIR/migrations/20260621_04_agent_attendance_settings.sql"
  "$SUPABASE_DIR/migrations/20260621_05_conversation_inactivity_timers.sql"
  "$SUPABASE_DIR/migrations/20260621_06_messages_authorship.sql"
  "$SUPABASE_DIR/migrations/20260621_07_messages_company_id.sql"
  "$SUPABASE_DIR/migrations/20260621_08_conversations_status_constraints.sql"
  "$SUPABASE_DIR/migrations/20260621_08b_conversations_status_validate.sql"
  "$SUPABASE_DIR/migrations/20260621_90_concurrent_indexes.sql"
  "$SUPABASE_DIR/migrations/20260621_99_rls_attendance_tables.sql"
  "$SUPABASE_DIR/migrations/20260622_attendance_transition_rpc.sql"
  "$SUPABASE_DIR/migrations/20260623_01_fix_inactivity_timer_processing_status.sql"
  "$SUPABASE_DIR/migrations/20260623_02_sla_worker_indexes.sql"
  "$SUPABASE_DIR/migrations/20260623_03_conversations_session_id_multitenant_unique.sql"
  "$SUPABASE_DIR/migrations/20260624_atomic_balance_rpcs.sql"
  "$SUPABASE_DIR/migrations/20260624_auth_lockout_columns.sql"
  "$SUPABASE_DIR/migrations/20260624_mcp_oauth_clients_rls.sql"
  "$SUPABASE_DIR/migrations/20260624_platform_settings_rls.sql"
  "$SUPABASE_DIR/migrations/20260624_revoke_anon_attendance.sql"
  "$SUPABASE_DIR/migrations/20260625_01_whatsapp_provider_seam.sql"
  "$SUPABASE_DIR/migrations/20260625_02_whatsapp_seam_deactivate_orphans.sql"
  "$SUPABASE_DIR/migrations/20260625_03_whatsapp_seam_unique_index.sql"
  "$SUPABASE_DIR/migrations/20260626_01_billing_idempotency_keys.sql"
  "$SUPABASE_DIR/migrations/20260626_01_integrations_webhook_token.sql"
  "$SUPABASE_DIR/migrations/20260626_02_token_usage_outbox.sql"
  "$SUPABASE_DIR/migrations/20260626_03_billing_rpcs.sql"
  "$SUPABASE_DIR/migrations/20260626_04_revoke_debit_company_balance.sql"
  "$SUPABASE_DIR/migrations/20260627_01_rpc_list_contacts.sql"
  "$SUPABASE_DIR/migrations/20260627_02_rpc_metrics.sql"
  "$SUPABASE_DIR/migrations/20260627_03_idx_conversations_company_created.sql"
  "$SUPABASE_DIR/migrations/20260627_04_rpc_metrics_leads_fix.sql"
  "$SUPABASE_DIR/migrations/20260628_01_company_attendance_settings.sql"
  "$SUPABASE_DIR/migrations/20260628_02_openrouter_models_refresh.sql"
  "$SUPABASE_DIR/migrations/20260628_03_platform_provider_alerts.sql"
  "$SUPABASE_DIR/migrations/20260628_04_rpc_metrics_attendance_agents.sql"
  "$SUPABASE_DIR/seed_llm_pricing.sql"
  "$SUPABASE_DIR/seed_platform_settings.sql"
)

case "$MODE" in
  fresh) files=("${fresh_files[@]}") ;;
  upgrade) files=("${upgrade_files[@]}") ;;
  *)
    echo "usage: $0 [fresh|upgrade]" >&2
    echo "fresh: new Agent Smith install using schema_completo_v7.0.sql" >&2
    echo "upgrade: existing v6.2 install using dated migrations in order" >&2
    exit 2
    ;;
esac

[ -d "$SUPABASE_DIR" ] || die "Supabase directory not found: $SUPABASE_DIR"

printf 'Supabase setup mode: %s\n' "$MODE"
printf 'Files to apply:\n'
for file in "${files[@]}"; do
  [ -f "$file" ] || die "missing SQL file: $file"
  printf '  - %s\n' "${file#$SUPABASE_DIR/}"
done

if [ "${CONFIRM:-}" != "1" ]; then
  echo
  echo "Dry run only. Set CONFIRM=1 to apply these files to SUPABASE_DB_URL."
  exit 0
fi

[ -n "${SUPABASE_DB_URL:-}" ] || die "set SUPABASE_DB_URL in $APP_ENV_FILE"

for file in "${files[@]}"; do
  run_psql "$file"
done

echo "Supabase setup complete."
