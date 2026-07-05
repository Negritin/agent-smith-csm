#!/usr/bin/env python3
"""Audit the Meta Cloud WhatsApp cutover state.

Phases:
  prepared  - current pre-secret state: history, static credentials, relay, and
              public webhook surface are ready. App Secret may still be missing.
  shadow    - Meta is pointing to Agent Smith in shadow mode and real webhook
              events have been received/persisted.
  active    - Agent Smith is the active responder for the official Meta number.
"""

from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
HELPERS = runpy.run_path(str(SCRIPT_DIR / "activate-meta-cloud-whatsapp.py"))
load_env_file = HELPERS["load_env_file"]
fetch_json = HELPERS["fetch_json"]
sql_literal = HELPERS["sql_literal"]


def pass_(message: str) -> None:
    print(f"ok: {message}")


def warn(message: str) -> None:
    print(f"warn: {message}", file=sys.stderr)


def fail(message: str, failures: list[str]) -> None:
    print(f"fail: {message}", file=sys.stderr)
    failures.append(message)


def default_backend_url(args: argparse.Namespace) -> str:
    value = (
        args.backend_url
        or os.environ.get("NEXT_PUBLIC_API_URL")
        or os.environ.get("NEXT_PUBLIC_BACKEND_URL")
        or os.environ.get("BACKEND_URL")
    )
    if value:
        return value.rstrip("/")
    host = os.environ.get("AGENT_SMITH_API_HOST")
    if host:
        return f"https://{host}".rstrip("/")
    return "https://agent-smith-api.5.161.73.5.sslip.io"


def resolve_integration_id(database_url: str, requested: str | None) -> str:
    if requested:
        return requested
    sql = """
SELECT coalesce((
  SELECT id::text
    FROM public.integrations
   WHERE provider = 'meta-cloud'
   ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
   LIMIT 1
), '');
"""
    row = fetch_json(database_url, f"SELECT to_jsonb(({sql.rstrip(';')}))::text;")
    if isinstance(row, str) and row:
        return row
    raise SystemExit("No meta-cloud integration found; pass --integration-id")


def integration_state_sql(integration_id: str) -> str:
    integration = f"{sql_literal(integration_id)}::uuid"
    return f"""
SELECT coalesce((
  SELECT jsonb_build_object(
    'id', id::text,
    'provider', provider,
    'identifier_last4', right(regexp_replace(coalesce(identifier, ''), '\\D', '', 'g'), 4),
    'is_active', coalesce(is_active, false),
    'mode', whatsapp_webhook_mode,
    'has_phone_number_id', nullif(instance_id, '') IS NOT NULL,
    'has_access_token', nullif(token, '') IS NOT NULL,
    'has_app_secret', nullif(client_token, '') IS NOT NULL,
    'has_verify_token', nullif(coalesce(
      provider_config->>'webhook_verify_token',
      provider_config->>'meta_webhook_verify_token',
      ''
    ), '') IS NOT NULL,
    'has_waba_id', nullif(coalesce(
      provider_config->>'business_account_id',
      provider_config->>'waba_id',
      ''
    ), '') IS NOT NULL,
    'has_webhook_token', nullif(webhook_token, '') IS NOT NULL,
    'has_webhook_token_hash', nullif(webhook_token_hash, '') IS NOT NULL,
    'webhook_token_prefix', webhook_token_prefix,
    'relay_enabled', lower(coalesce(provider_config->>'chatwoot_relay_enabled', 'false'))
      IN ('true', '1', 'yes', 'on', 'enabled'),
    'relay_base_url_present', nullif(provider_config->>'chatwoot_relay_base_url', '') IS NOT NULL,
    'relay_phone_present', nullif(coalesce(provider_config->>'chatwoot_relay_phone_number', identifier, ''), '') IS NOT NULL,
    'imported_conversations', (
      SELECT count(*)
        FROM public.whatsapp_external_conversations
       WHERE integration_id = public.integrations.id
    ),
    'imported_messages', (
      SELECT count(*)
        FROM public.whatsapp_external_messages
       WHERE integration_id = public.integrations.id
         AND source = 'chatwoot'
         AND event_kind = 'message'
    ),
    'meta_webhook_messages', (
      SELECT count(*)
        FROM public.whatsapp_external_messages
       WHERE integration_id = public.integrations.id
         AND source = 'meta_webhook'
         AND event_kind = 'message'
    ),
    'meta_webhook_statuses', (
      SELECT count(*)
        FROM public.whatsapp_external_messages
       WHERE integration_id = public.integrations.id
         AND source = 'meta_webhook'
         AND event_kind = 'status'
    ),
    'shadow_media_persisted', (
      SELECT count(*)
        FROM public.whatsapp_external_messages
       WHERE integration_id = public.integrations.id
         AND source = 'meta_webhook'
         AND event_kind = 'message'
         AND nullif(media_metadata->>'stable_url', '') IS NOT NULL
    )
  )::text
  FROM public.integrations
  WHERE id = {integration}
    AND provider = 'meta-cloud'
), '');
"""


def check_health(backend_url: str, failures: list[str]) -> None:
    url = f"{backend_url.rstrip('/')}/api/v1/webhook/meta-cloud/health"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if response.status == 200 and payload.get("webhook") == "meta-cloud":
                pass_("meta-cloud public webhook health")
                return
            fail("meta-cloud health returned unexpected payload", failures)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        fail(f"meta-cloud health unavailable: {exc}", failures)


def require_bool(state: dict[str, Any], key: str, label: str, failures: list[str]) -> None:
    if state.get(key):
        pass_(label)
    else:
        fail(label, failures)


def require_count_at_least(
    state: dict[str, Any],
    key: str,
    label: str,
    minimum: int,
    failures: list[str],
) -> None:
    count = int(state.get(key) or 0)
    if count >= minimum:
        pass_(f"{label} count {count}")
    else:
        fail(f"{label} count {count} below {minimum}", failures)


def audit_phase(state: dict[str, Any], phase: str, failures: list[str]) -> None:
    require_bool(state, "has_phone_number_id", "phone_number_id present", failures)
    require_bool(state, "has_access_token", "access token present", failures)
    require_bool(state, "has_verify_token", "webhook verify token present", failures)
    require_bool(state, "has_waba_id", "WABA/business account id present", failures)
    require_bool(state, "has_webhook_token", "Agent Smith webhook token present", failures)
    require_bool(state, "has_webhook_token_hash", "Agent Smith webhook token hash present", failures)
    require_count_at_least(state, "imported_conversations", "Chatwoot imported conversations", 1, failures)
    require_count_at_least(state, "imported_messages", "Chatwoot imported messages", 1, failures)
    require_bool(state, "relay_enabled", "Chatwoot relay enabled", failures)
    require_bool(state, "relay_base_url_present", "Chatwoot relay base URL present", failures)
    require_bool(state, "relay_phone_present", "Chatwoot relay phone present", failures)

    if phase == "prepared":
        if state.get("has_app_secret"):
            pass_("Meta App Secret present")
        else:
            warn("Meta App Secret still missing; prepared phase can pass, shadow/active cannot")
        if not state.get("is_active") and state.get("mode") == "shadow":
            pass_("integration parked inactive in shadow until App Secret is entered")
        return

    require_bool(state, "has_app_secret", "Meta App Secret present", failures)
    require_bool(state, "is_active", "meta-cloud integration active", failures)
    expected_mode = "active" if phase == "active" else "shadow"
    if state.get("mode") == expected_mode:
        pass_(f"webhook mode is {expected_mode}")
    else:
        fail(f"webhook mode is {state.get('mode')}, expected {expected_mode}", failures)
    require_count_at_least(state, "meta_webhook_messages", "live Meta webhook messages", 1, failures)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("prepared", "shadow", "active"), default="prepared")
    parser.add_argument("--integration-id", default=os.environ.get("META_CLOUD_INTEGRATION_ID"))
    parser.add_argument("--backend-url")
    parser.add_argument(
        "--app-env-file",
        default=os.environ.get("APP_ENV_FILE", "/opt/agent-smith/.env.app"),
    )
    args = parser.parse_args()

    load_env_file(args.app_env_file)
    database_url = os.environ.get("SUPABASE_DB_URL")
    if not database_url:
        raise SystemExit("SUPABASE_DB_URL is required")

    integration_id = resolve_integration_id(database_url, args.integration_id)
    state = fetch_json(database_url, integration_state_sql(integration_id))
    if not isinstance(state, dict):
        raise SystemExit("Meta Cloud integration not found")

    print(
        "meta_cloud_cutover "
        f"phase={args.phase} "
        f"integration_id={state['id']} "
        f"identifier_last4={state.get('identifier_last4') or ''} "
        f"mode={state.get('mode')} "
        f"active={str(state.get('is_active')).lower()} "
        f"token_prefix={state.get('webhook_token_prefix') or ''}"
    )

    failures: list[str] = []
    check_health(default_backend_url(args), failures)
    audit_phase(state, args.phase, failures)

    if failures:
        print(f"fail: Meta Cloud cutover {args.phase} audit failed", file=sys.stderr)
        return 1

    pass_(f"Meta Cloud cutover {args.phase} audit complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
