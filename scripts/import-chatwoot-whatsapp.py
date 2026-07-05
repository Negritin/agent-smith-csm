#!/usr/bin/env python3
"""Import a Chatwoot WhatsApp inbox into Agent Smith.

Reads Chatwoot locally through ``docker exec ... rails runner`` and writes to
Supabase/Postgres through ``psql``. The import is idempotent through
``whatsapp_external_conversations`` and ``whatsapp_external_messages``.

Required env:
  SUPABASE_DB_URL

Required args:
  --inbox-id, --company-id, --agent-id, --integration-id
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from typing import Any


RAILS_EXPORTER = r"""
require "json"
inbox_id = ENV.fetch("CHATWOOT_INBOX_ID").to_i
limit = ENV["CHATWOOT_LIMIT"].to_i
scope = Conversation.where(inbox_id: inbox_id).includes(:contact, :contact_inbox)
scope = scope.order(:id)
scope = scope.limit(limit) if limit > 0
scope.find_each do |conversation|
  contact = conversation.contact
  contact_inbox = conversation.contact_inbox
  puts JSON.generate({
    kind: "conversation",
    id: conversation.id.to_s,
    uuid: conversation.uuid.to_s,
    status: conversation.status,
    created_at: conversation.created_at&.iso8601,
    updated_at: conversation.updated_at&.iso8601,
    last_activity_at: conversation.last_activity_at&.iso8601,
    contact_id: contact&.id&.to_s,
    contact_name: contact&.name,
    contact_phone: contact&.phone_number,
    source_id: contact_inbox&.source_id,
    raw: {
      account_id: conversation.account_id,
      inbox_id: conversation.inbox_id,
      display_id: conversation.display_id,
      identifier: conversation.identifier,
      custom_attributes: conversation.custom_attributes,
      additional_attributes: conversation.additional_attributes
    }
  })
  conversation.messages.includes(:attachments).order(:created_at, :id).find_each do |message|
    next if message.private
    attachments = message.attachments.map do |attachment|
      {
        id: attachment.id.to_s,
        file_type: attachment.file_type,
        external_url: attachment.external_url,
        fallback_title: attachment.fallback_title,
        extension: attachment.extension,
        meta: attachment.meta
      }
    end
    puts JSON.generate({
      kind: "message",
      id: message.id.to_s,
      conversation_id: conversation.id.to_s,
      source_id: message.source_id,
      message_type: message.message_type,
      content: message.content,
      processed_message_content: message.processed_message_content,
      status: message.status,
      sender_type: message.sender_type,
      sender_id: message.sender_id&.to_s,
      created_at: message.created_at&.iso8601,
      updated_at: message.updated_at&.iso8601,
      attachments: attachments,
      raw: {
        account_id: message.account_id,
        inbox_id: message.inbox_id,
        content_type: message.content_type,
        content_attributes: message.content_attributes,
        external_source_ids: message.external_source_ids,
        additional_attributes: message.additional_attributes
      }
    })
  end
end
"""


def shell(args: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def discover_chatwoot_container() -> str:
    proc = shell(["docker", "ps", "--format", "{{.Names}}\t{{.Image}}"])
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.strip() or "docker ps failed")
    for line in proc.stdout.splitlines():
        name, _, image = line.partition("\t")
        if image.startswith("chatwoot/chatwoot") and "sidekiq" not in name:
            return name
    raise SystemExit("Chatwoot web container not found; pass --chatwoot-container")


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def json_literal(value: Any) -> str:
    return sql_literal(json.dumps(value if value is not None else {}, ensure_ascii=False))


def uuid_literal(value: str) -> str:
    return f"{sql_literal(value)}::uuid"


def ts_literal(value: Any) -> str:
    return f"{sql_literal(value)}::timestamptz" if value else "now()"


def split_name(name: str | None) -> tuple[str, str]:
    clean = (name or "").strip()
    if not clean:
        return "Usuário", "WhatsApp"
    first, _, rest = clean.partition(" ")
    return first[:100] or "Usuário", (rest[:100] if rest else "WhatsApp")


def message_direction(message_type: Any) -> str:
    return "inbound" if str(message_type) == "incoming" or str(message_type) == "0" else "outbound"


def media_fields(attachments: list[dict[str, Any]]) -> tuple[str, str | None, str | None, dict[str, Any]]:
    if not attachments:
        return "text", None, None, {}
    first = attachments[0]
    file_type = str(first.get("file_type") or "").lower()
    url = first.get("external_url")
    metadata = {"attachments": attachments}
    if "audio" in file_type or file_type in {"1", "voice"}:
        return "voice", url, None, metadata
    if "image" in file_type or file_type in {"0", "image"}:
        return "text", None, url, metadata
    return "text", None, None, metadata


def build_sql(records: list[dict[str, Any]], args: argparse.Namespace) -> str:
    conversations = [r for r in records if r.get("kind") == "conversation"]
    messages = [r for r in records if r.get("kind") == "message"]
    conversation_by_id = {c["id"]: c for c in conversations}
    out = ["BEGIN;"]
    provider = "meta-cloud"
    source = "chatwoot"
    sentinel = "00000000-0000-0000-0000-000000000000"

    for conv in conversations:
        phone = conv.get("contact_phone") or conv.get("source_id") or f"chatwoot:{conv['id']}"
        first_name, last_name = split_name(conv.get("contact_name"))
        email = f"{phone}_{args.company_id}@whatsapp.smith.ai"
        session_id = f"whatsapp:{phone}:{args.company_id}:{args.agent_id}"
        preview = "Histórico importado do Chatwoot"
        external_conv_id = f"chatwoot:{conv['id']}"
        out.append(
            f"""
WITH upsert_user AS (
  INSERT INTO public.users_v2 (
    email, phone, company_id, status, first_name, last_name, cpf, birth_date,
    terms_accepted_at, privacy_policy_accepted_at
  )
  VALUES (
    {sql_literal(email)}, {sql_literal(phone)}, {uuid_literal(args.company_id)}, 'lead',
    {sql_literal(first_name)}, {sql_literal(last_name)},
    left(md5({sql_literal(phone + '_' + args.company_id)}), 14),
    '2000-01-01', now(), now()
  )
  ON CONFLICT (email) DO UPDATE
    SET phone = EXCLUDED.phone,
        company_id = EXCLUDED.company_id,
        first_name = EXCLUDED.first_name,
        last_name = EXCLUDED.last_name
  RETURNING id
), existing_conversation AS (
  SELECT id
    FROM public.conversations
   WHERE company_id = {uuid_literal(args.company_id)}
     AND coalesce(agent_id, {uuid_literal(sentinel)}) = {uuid_literal(args.agent_id)}
     AND session_id = {sql_literal(session_id)}
   LIMIT 1
), inserted_conversation AS (
  INSERT INTO public.conversations (
    company_id, user_id, session_id, agent_id, channel, status,
    last_message_preview, last_message_at, user_name, user_phone,
    agent_name, status_color, created_at, updated_at
  )
  SELECT
    {uuid_literal(args.company_id)}, upsert_user.id, {sql_literal(session_id)},
    {uuid_literal(args.agent_id)}, 'whatsapp', 'open',
    {sql_literal(preview)}, {ts_literal(conv.get("last_activity_at") or conv.get("updated_at"))},
    {sql_literal(conv.get("contact_name") or "Usuário WhatsApp")}, {sql_literal(phone)},
    'Smith Agent', 'green', {ts_literal(conv.get("created_at"))}, {ts_literal(conv.get("updated_at"))}
  FROM upsert_user
  WHERE NOT EXISTS (SELECT 1 FROM existing_conversation)
  RETURNING id
), selected_conversation AS (
  SELECT id FROM existing_conversation
  UNION ALL
  SELECT id FROM inserted_conversation
  LIMIT 1
)
INSERT INTO public.whatsapp_external_conversations (
  company_id, integration_id, conversation_id, provider, source,
  external_conversation_id, external_contact_id, wa_phone, raw_payload
)
SELECT
  {uuid_literal(args.company_id)}, {uuid_literal(args.integration_id)}, selected_conversation.id,
  {sql_literal(provider)}, {sql_literal(source)}, {sql_literal(external_conv_id)},
  {sql_literal(conv.get("contact_id"))}, {sql_literal(phone)}, {json_literal(conv.get("raw"))}::jsonb
FROM selected_conversation
ON CONFLICT (provider, source, external_conversation_id) DO UPDATE
  SET conversation_id = EXCLUDED.conversation_id,
      integration_id = EXCLUDED.integration_id,
      raw_payload = EXCLUDED.raw_payload,
      updated_at = now();
"""
        )

    for msg in messages:
        conv = conversation_by_id.get(msg.get("conversation_id"), {})
        phone = conv.get("contact_phone") or conv.get("source_id")
        external_conv_id = f"chatwoot:{msg['conversation_id']}"
        external_msg_id = f"chatwoot:{msg['id']}"
        direction = message_direction(msg.get("message_type"))
        role = "user" if direction == "inbound" else "assistant"
        attachments = msg.get("attachments") or []
        msg_type, audio_url, image_url, media_metadata = media_fields(attachments)
        content = (
            msg.get("processed_message_content")
            or msg.get("content")
            or ("[Mídia importada]" if attachments else "")
        )
        if not content:
            continue
        out.append(
            f"""
WITH selected_conversation AS (
  SELECT conversation_id
    FROM public.whatsapp_external_conversations
   WHERE provider = {sql_literal(provider)}
     AND source = {sql_literal(source)}
     AND external_conversation_id = {sql_literal(external_conv_id)}
   LIMIT 1
), existing_external AS (
  SELECT message_id
    FROM public.whatsapp_external_messages
   WHERE provider = {sql_literal(provider)}
     AND external_message_id = {sql_literal(external_msg_id)}
     AND event_kind = 'message'
   LIMIT 1
), inserted_message AS (
  INSERT INTO public.messages (
    conversation_id, company_id, role, content, type, audio_url, image_url,
    author_type, created_at
  )
  SELECT
    selected_conversation.conversation_id, {uuid_literal(args.company_id)},
    {sql_literal(role)}, {sql_literal(content)}, {sql_literal(msg_type)},
    {sql_literal(audio_url)}, {sql_literal(image_url)},
    {sql_literal('customer' if role == 'user' else 'human_operator')},
    {ts_literal(msg.get("created_at"))}
  FROM selected_conversation
  WHERE NOT EXISTS (SELECT 1 FROM existing_external)
  RETURNING id
), selected_message AS (
  SELECT message_id AS id FROM existing_external WHERE message_id IS NOT NULL
  UNION ALL
  SELECT id FROM inserted_message
  LIMIT 1
)
INSERT INTO public.whatsapp_external_messages (
  company_id, integration_id, conversation_id, message_id, provider, source,
  event_kind, external_message_id, external_conversation_id, direction, status,
  wa_from, wa_to, message_type, content, media_metadata, raw_payload,
  provider_timestamp
)
SELECT
  {uuid_literal(args.company_id)}, {uuid_literal(args.integration_id)},
  selected_conversation.conversation_id, selected_message.id,
  {sql_literal(provider)}, {sql_literal(source)}, 'message',
  {sql_literal(external_msg_id)}, {sql_literal(external_conv_id)},
  {sql_literal(direction)}, {sql_literal(str(msg.get("status") or ""))},
  {sql_literal(phone if direction == "inbound" else None)},
  {sql_literal(phone if direction != "inbound" else None)},
  {sql_literal(msg_type)}, {sql_literal(content)},
  {json_literal(media_metadata)}::jsonb, {json_literal(msg.get("raw"))}::jsonb,
  extract(epoch from {ts_literal(msg.get("created_at"))})::bigint
FROM selected_conversation, selected_message
ON CONFLICT (provider, external_message_id, event_kind) DO UPDATE
  SET message_id = EXCLUDED.message_id,
      conversation_id = EXCLUDED.conversation_id,
      status = EXCLUDED.status,
      raw_payload = EXCLUDED.raw_payload,
      updated_at = now();
"""
        )

    out.append("COMMIT;")
    return "\n".join(out)


def export_chatwoot(args: argparse.Namespace) -> list[dict[str, Any]]:
    container = args.chatwoot_container or discover_chatwoot_container()
    cmd = [
        "docker",
        "exec",
        "-e",
        f"CHATWOOT_INBOX_ID={args.inbox_id}",
        "-e",
        f"CHATWOOT_LIMIT={args.limit or 0}",
        container,
        "bundle",
        "exec",
        "rails",
        "runner",
        RAILS_EXPORTER,
    ]
    proc = shell(cmd)
    if proc.returncode != 0:
        raise SystemExit(proc.stderr)
    records: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        records.append(json.loads(line))
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inbox-id", required=True)
    parser.add_argument("--company-id", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--integration-id", required=True)
    parser.add_argument("--chatwoot-container")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        raise SystemExit("SUPABASE_DB_URL is required")

    records = export_chatwoot(args)
    conv_count = sum(1 for r in records if r.get("kind") == "conversation")
    msg_count = sum(1 for r in records if r.get("kind") == "message")
    print(f"chatwoot_export conversations={conv_count} messages={msg_count}", file=sys.stderr)

    sql = build_sql(records, args)
    if args.dry_run:
        print(sql)
        return 0

    proc = subprocess.run(
        ["psql", db_url, "-v", "ON_ERROR_STOP=1", "-q"],
        input=sql,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        print(proc.stdout, file=sys.stderr)
        print(proc.stderr, file=sys.stderr)
        return proc.returncode
    print(f"import_complete conversations={conv_count} messages={msg_count}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
