-- ============================================================================
-- WhatsApp Meta Cloud provider + external history sidecars.
--
-- Adds the official WABA/Cloud API provider to the same per-agent integration
-- model used by z-api/uazapi/evolution, without moving secrets to global env.
-- ============================================================================

ALTER TABLE public.integrations
    ADD COLUMN IF NOT EXISTS provider_config jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE public.integrations
    ADD COLUMN IF NOT EXISTS whatsapp_webhook_mode text NOT NULL DEFAULT 'active';

ALTER TABLE public.integrations
    DROP CONSTRAINT IF EXISTS integrations_whatsapp_webhook_mode_check;

ALTER TABLE public.integrations
    ADD CONSTRAINT integrations_whatsapp_webhook_mode_check
    CHECK (whatsapp_webhook_mode IN ('shadow', 'active'));

COMMENT ON COLUMN public.integrations.provider_config IS
    'Non-secret provider metadata. For meta-cloud: business_account_id/WABA id, graph_version, webhook_verify_token, chatwoot channel/inbox ids, template sync metadata. Never store access tokens or app secrets here.';

COMMENT ON COLUMN public.integrations.whatsapp_webhook_mode IS
    'Official WhatsApp webhook execution mode. shadow = verify and persist external events only; active = persist and dispatch to the Agent Smith turn pipeline.';

DROP INDEX IF EXISTS public.uniq_whatsapp_active_integration_per_agent;

CREATE UNIQUE INDEX uniq_whatsapp_active_integration_per_agent
    ON public.integrations (agent_id)
    WHERE provider IN ('z-api', 'uazapi', 'evolution', 'meta-cloud')
      AND agent_id IS NOT NULL
      AND is_active = true;

COMMENT ON INDEX public.uniq_whatsapp_active_integration_per_agent IS
    'Exclusividade WhatsApp: no máximo UMA integração WhatsApp ATIVA por agente, sobre o conjunto canônico {z-api, uazapi, evolution, meta-cloud}. Parcial em is_active=true — linhas inativas (histórico) e globais (agent_id IS NULL) não colidem.';

CREATE TABLE IF NOT EXISTS public.whatsapp_external_conversations (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    company_id uuid NOT NULL,
    integration_id uuid REFERENCES public.integrations(id) ON DELETE SET NULL,
    conversation_id uuid REFERENCES public.conversations(id) ON DELETE SET NULL,
    provider text NOT NULL,
    source text NOT NULL,
    external_conversation_id text NOT NULL,
    external_contact_id text,
    wa_phone text,
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    imported_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_whatsapp_external_conversations_source_id
    ON public.whatsapp_external_conversations (provider, source, external_conversation_id);

CREATE INDEX IF NOT EXISTS idx_whatsapp_external_conversations_company
    ON public.whatsapp_external_conversations (company_id, integration_id);

CREATE TABLE IF NOT EXISTS public.whatsapp_external_messages (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    company_id uuid NOT NULL,
    integration_id uuid REFERENCES public.integrations(id) ON DELETE SET NULL,
    conversation_id uuid REFERENCES public.conversations(id) ON DELETE SET NULL,
    message_id uuid REFERENCES public.messages(id) ON DELETE SET NULL,
    provider text NOT NULL,
    source text NOT NULL,
    event_kind text NOT NULL,
    external_message_id text NOT NULL,
    external_conversation_id text,
    direction text,
    status text,
    wa_from text,
    wa_to text,
    message_type text,
    content text,
    media_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    provider_timestamp bigint,
    imported_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT whatsapp_external_messages_event_kind_check
        CHECK (event_kind IN ('message', 'status')),
    CONSTRAINT whatsapp_external_messages_direction_check
        CHECK (
            direction IS NULL OR direction IN ('inbound', 'outbound', 'outbound_echo')
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_whatsapp_external_messages_provider_id
    ON public.whatsapp_external_messages (provider, external_message_id, event_kind);

CREATE INDEX IF NOT EXISTS idx_whatsapp_external_messages_company
    ON public.whatsapp_external_messages (company_id, integration_id);

CREATE INDEX IF NOT EXISTS idx_whatsapp_external_messages_conversation
    ON public.whatsapp_external_messages (conversation_id, provider_timestamp);

COMMENT ON TABLE public.whatsapp_external_conversations IS
    'Mapping table for imported/mirrored WhatsApp conversations from Chatwoot or Meta Cloud into Agent Smith conversations.';

COMMENT ON TABLE public.whatsapp_external_messages IS
    'Idempotent sidecar for provider message ids, delivery statuses, media metadata, and raw webhook/import payloads. Used by Meta Cloud webhooks and Chatwoot backfill.';
