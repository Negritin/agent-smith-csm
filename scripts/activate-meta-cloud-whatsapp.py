#!/usr/bin/env python3
"""Activate an existing Meta Cloud WhatsApp integration safely.

The App Secret is intentionally read from a prompt (or META_APP_SECRET) instead
of a command-line argument, so it does not land in shell history or ``ps``.

Required:
  --integration-id <uuid>

Optional:
  --mode shadow|active  (default: shadow)
  --confirm-active     (required for --mode active)
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


DEFAULT_APP_ENV_FILE = "/opt/agent-smith/.env.app"
DEFAULT_BACKEND_URL = "https://agent-smith-api.5.161.73.5.sslip.io"
WHATSAPP_PROVIDERS = ("z-api", "uazapi", "evolution", "meta-cloud")


def load_env_file(path: str) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _pgpass_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")


def psql_env_from_url(database_url: str) -> tuple[list[str], dict[str, str], str | None]:
    """Build a psql invocation without putting the DB URL/password in argv."""
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise SystemExit("SUPABASE_DB_URL must use postgres/postgresql scheme")

    host = parsed.hostname or ""
    port = str(parsed.port or 5432)
    database = unquote((parsed.path or "/postgres").lstrip("/") or "postgres")
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    query = parse_qs(parsed.query or "")
    sslmode = (query.get("sslmode") or ["require"])[0]

    env = os.environ.copy()
    env.update(
        {
            "PGHOST": host,
            "PGPORT": port,
            "PGDATABASE": database,
            "PGUSER": user,
            "PGSSLMODE": sslmode,
            "PGCONNECT_TIMEOUT": "15",
        }
    )

    pgpass_path = None
    if password:
        fd, pgpass_path = tempfile.mkstemp(prefix="agent-smith-pgpass-")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(
                ":".join(
                    [
                        _pgpass_escape(host),
                        _pgpass_escape(port),
                        _pgpass_escape(database),
                        _pgpass_escape(user),
                        _pgpass_escape(password),
                    ]
                )
                + "\n"
            )
        os.chmod(pgpass_path, 0o600)
        env["PGPASSFILE"] = pgpass_path

    return ["psql"], env, pgpass_path


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    text = str(value).replace("\x00", "")
    suffix = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    tag = f"as_{suffix}"
    while f"${tag}$" in text:
        suffix = hashlib.sha256((suffix + text).encode("utf-8")).hexdigest()[:16]
        tag = f"as_{suffix}"
    return f"${tag}${text}${tag}$"


def run_psql(database_url: str, sql: str) -> str:
    psql_base, psql_env, pgpass_path = psql_env_from_url(database_url)
    try:
        proc = subprocess.run(
            [*psql_base, "-X", "-v", "ON_ERROR_STOP=1", "-qAt"],
            input=sql,
            env=psql_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    finally:
        if pgpass_path:
            try:
                os.unlink(pgpass_path)
            except FileNotFoundError:
                pass

    if proc.returncode != 0:
        if proc.stdout:
            print(proc.stdout, file=sys.stderr)
        if proc.stderr:
            print(proc.stderr, file=sys.stderr)
        raise SystemExit(proc.returncode)
    return proc.stdout.strip()


def fetch_json(database_url: str, sql: str) -> dict[str, Any] | list[Any] | None:
    output = run_psql(database_url, sql)
    if not output:
        return None
    return json.loads(output)


def integration_state_sql(integration_id: str) -> str:
    integration = f"{sql_literal(integration_id)}::uuid"
    providers = ", ".join(sql_literal(provider) for provider in WHATSAPP_PROVIDERS)
    return f"""
WITH target AS (
  SELECT *
    FROM public.integrations
   WHERE id = {integration}
   LIMIT 1
), active_conflicts AS (
  SELECT id, provider, identifier, webhook_token_prefix
    FROM public.integrations
   WHERE provider IN ({providers})
     AND coalesce(is_active, false)
     AND agent_id = (SELECT agent_id FROM target)
     AND id <> {integration}
)
SELECT coalesce((
  SELECT jsonb_build_object(
    'id', target.id::text,
    'provider', target.provider,
    'company_id', target.company_id::text,
    'agent_id', target.agent_id::text,
    'identifier', target.identifier,
    'identifier_last4', right(regexp_replace(coalesce(target.identifier, ''), '\\D', '', 'g'), 4),
    'phone_number_id_present', nullif(target.instance_id, '') IS NOT NULL,
    'access_token_present', nullif(target.token, '') IS NOT NULL,
    'app_secret_present', nullif(target.client_token, '') IS NOT NULL,
    'webhook_token_present', nullif(target.webhook_token, '') IS NOT NULL,
    'webhook_token_hash_present', nullif(target.webhook_token_hash, '') IS NOT NULL,
    'webhook_token_prefix', target.webhook_token_prefix,
    'webhook_verify_token_present',
      nullif(coalesce(
        target.provider_config->>'webhook_verify_token',
        target.provider_config->>'meta_webhook_verify_token',
        ''
      ), '') IS NOT NULL,
    'business_account_id_present',
      nullif(coalesce(
        target.provider_config->>'business_account_id',
        target.provider_config->>'waba_id',
        ''
      ), '') IS NOT NULL,
    'mode', target.whatsapp_webhook_mode,
    'is_active', coalesce(target.is_active, false),
    'active_conflicts', coalesce((
      SELECT jsonb_agg(jsonb_build_object(
        'id', id::text,
        'provider', provider,
        'identifier', identifier,
        'webhook_token_prefix', webhook_token_prefix
      ))
        FROM active_conflicts
    ), '[]'::jsonb)
  )::text
  FROM target
), '');
"""


def update_sql(integration_id: str, app_secret: str, mode: str) -> str:
    integration = f"{sql_literal(integration_id)}::uuid"
    return f"""
UPDATE public.integrations
   SET client_token = {sql_literal(app_secret)},
       whatsapp_webhook_mode = {sql_literal(mode)},
       is_active = true,
       provider_config = (
         (coalesce(provider_config, '{{}}'::jsonb) - 'activation_blocker')
         || jsonb_build_object(
              'activation_state', 'ready',
              'activation_mode', {sql_literal(mode)},
              'activation_ready_at', to_char(now(), 'YYYY-MM-DD"T"HH24:MI:SSOF')
            )
       ),
       updated_at = now()
 WHERE id = {integration}
   AND provider = 'meta-cloud'
RETURNING jsonb_build_object(
  'id', id::text,
  'provider', provider,
  'identifier_last4', right(regexp_replace(coalesce(identifier, ''), '\\D', '', 'g'), 4),
  'mode', whatsapp_webhook_mode,
  'is_active', coalesce(is_active, false),
  'webhook_token_prefix', webhook_token_prefix,
  'webhook_token', webhook_token,
  'webhook_verify_token', provider_config->>'webhook_verify_token'
)::text;
"""


def require_ready_for_activation(state: dict[str, Any]) -> None:
    if state.get("provider") != "meta-cloud":
        raise SystemExit("Integration is not provider=meta-cloud")

    missing = []
    checks = {
        "phone_number_id": state.get("phone_number_id_present"),
        "access_token": state.get("access_token_present"),
        "webhook_token": state.get("webhook_token_present"),
        "webhook_token_hash": state.get("webhook_token_hash_present"),
        "webhook_verify_token": state.get("webhook_verify_token_present"),
        "business_account_id/WABA": state.get("business_account_id_present"),
    }
    for label, ok in checks.items():
        if not ok:
            missing.append(label)

    conflicts = state.get("active_conflicts") or []
    if conflicts:
        conflict_labels = ", ".join(
            f"{item.get('provider')}:{item.get('identifier') or item.get('id')}"
            for item in conflicts
        )
        raise SystemExit(
            "Another active WhatsApp integration already exists for this agent: "
            + conflict_labels
        )

    if missing:
        raise SystemExit("Meta Cloud integration is missing: " + ", ".join(missing))


def resolve_backend_url(args: argparse.Namespace) -> str:
    value = (
        args.backend_url
        or os.environ.get("NEXT_PUBLIC_API_URL")
        or os.environ.get("NEXT_PUBLIC_BACKEND_URL")
        or os.environ.get("BACKEND_URL")
    )
    return (value or DEFAULT_BACKEND_URL).rstrip("/")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--integration-id", required=True)
    parser.add_argument("--mode", choices=("shadow", "active"), default="shadow")
    parser.add_argument(
        "--confirm-active",
        action="store_true",
        help="Required when --mode active. Active lets Agent Smith respond to WhatsApp.",
    )
    parser.add_argument(
        "--app-env-file",
        default=os.environ.get("APP_ENV_FILE", DEFAULT_APP_ENV_FILE),
        help="Env file to read SUPABASE_DB_URL from when it is not already exported.",
    )
    parser.add_argument("--backend-url")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--print-webhook-url",
        action="store_true",
        help="Prints the full callback URL. This includes the secret webhook token.",
    )
    args = parser.parse_args()

    load_env_file(args.app_env_file)
    database_url = os.environ.get("SUPABASE_DB_URL")
    if not database_url:
        raise SystemExit("SUPABASE_DB_URL is required")

    if args.mode == "active" and not args.confirm_active:
        raise SystemExit("--mode active requires --confirm-active")

    state = fetch_json(database_url, integration_state_sql(args.integration_id))
    if not state:
        raise SystemExit("Meta Cloud integration not found")
    if not isinstance(state, dict):
        raise SystemExit("Unexpected integration state response")

    require_ready_for_activation(state)

    if args.dry_run:
        print(
            "dry_run_ok "
            f"integration_id={state['id']} "
            f"active={str(state['is_active']).lower()} "
            f"current_mode={state['mode']} "
            f"target_mode={args.mode} "
            f"token_prefix={state.get('webhook_token_prefix') or ''}"
        )
        return 0

    app_secret = os.environ.get("META_APP_SECRET", "").strip()
    if not app_secret:
        app_secret = getpass.getpass("Meta App Secret: ").strip()
    if len(app_secret) < 8:
        raise SystemExit("Meta App Secret is missing or too short")

    updated = fetch_json(database_url, update_sql(args.integration_id, app_secret, args.mode))
    if not updated or not isinstance(updated, dict):
        raise SystemExit("Meta Cloud integration update did not return a row")

    print(
        "meta_cloud_activation_complete "
        f"integration_id={updated['id']} "
        f"active={str(updated['is_active']).lower()} "
        f"mode={updated['mode']} "
        f"identifier_last4={updated.get('identifier_last4') or ''} "
        f"token_prefix={updated.get('webhook_token_prefix') or ''}"
    )

    if args.print_webhook_url:
        backend = resolve_backend_url(args)
        token = updated.get("webhook_token")
        verify_token = updated.get("webhook_verify_token")
        print(f"callback_url={backend}/api/v1/webhook/meta-cloud/{token}")
        if verify_token:
            print(f"verify_token={verify_token}")
    else:
        print("full_webhook_url=hidden; use admin UI or rerun with --print-webhook-url")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
