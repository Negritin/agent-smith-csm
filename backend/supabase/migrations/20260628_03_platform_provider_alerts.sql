-- =============================================================================
-- 20260628_03_platform_provider_alerts.sql
-- Date: 2026-06-28
-- Purpose: Platform-wide LLM-provider health alerts (out-of-balance / quota).
--   When a provider (Anthropic/OpenAI/Google/OpenRouter) rejects a chat turn
--   because the PLATFORM account is out of credits/quota, the backend (service
--   role) raises a row here; a clean turn for that provider auto-resolves it.
--   Surfaced ONLY as a red banner in the master admin — never to tenants
--   (the LLM keys are platform-wide, not BYO per company).
--
-- IDEMPOTENT: CREATE TABLE IF NOT EXISTS + idempotent REVOKE/ENABLE.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.platform_provider_alerts (
    provider     TEXT PRIMARY KEY,           -- 'anthropic' | 'openai' | 'google' | 'openrouter'
    kind         TEXT NOT NULL DEFAULT 'balance',
    message      TEXT,                        -- short, truncated provider error (no PII)
    detected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at  TIMESTAMPTZ,                 -- NULL = active alert
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE public.platform_provider_alerts IS
  'Platform-wide LLM provider health alerts (out-of-balance/quota). Master-only: written by the backend service role on a balance error during a chat turn, auto-resolved when a turn for that provider succeeds, read by the master admin banner. NOT tenant-scoped.';

-- RLS: locked to service_role only. The backend writes via service role; the
-- Next.js master route reads via service role AFTER gating on the master admin
-- session. No anon/authenticated access (mirrors platform_settings hardening).
ALTER TABLE public.platform_provider_alerts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.platform_provider_alerts FROM anon, authenticated;
-- Explicit service_role GRANT follows the repo convention (cf.
-- 20260624_platform_settings_rls.sql / 20260621_99 template): the backend writes
-- and the Next.js master route reads via service_role. Idempotent, behavior-neutral.
GRANT ALL ON public.platform_provider_alerts TO service_role;

COMMIT;
