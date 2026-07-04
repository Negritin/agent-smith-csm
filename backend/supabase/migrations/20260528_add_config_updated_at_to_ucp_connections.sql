-- Cache fingerprint - add config_updated_at to ucp_connections.
--
-- Context:
-- The Tool Registry cache must be invalidated only when the *configuration* of a
-- UCP connection changes (is_active, store_url, manifest_version,
-- preferred_transport, capabilities_enabled). High-churn columns such as
-- last_used_at and last_error must NOT bust the cache, otherwise every request
-- would trigger a needless reload.
--
-- A dedicated config_updated_at column plus a conditional BEFORE UPDATE trigger
-- keeps the fingerprint stable across operational noise. clock_timestamp() is used
-- (instead of now()) so the value advances even within a single transaction.
--
-- Idempotency:
-- ADD COLUMN IF NOT EXISTS + CREATE OR REPLACE FUNCTION + DROP TRIGGER IF EXISTS
-- guarantee that re-running the migration does not raise.

BEGIN;

ALTER TABLE public.ucp_connections
    ADD COLUMN IF NOT EXISTS config_updated_at timestamptz NOT NULL DEFAULT now();

-- Defensive constraint enforcement for partial/older deploys.
ALTER TABLE public.ucp_connections
    ALTER COLUMN config_updated_at SET DEFAULT now();

UPDATE public.ucp_connections
SET config_updated_at = now()
WHERE config_updated_at IS NULL;

ALTER TABLE public.ucp_connections
    ALTER COLUMN config_updated_at SET NOT NULL;

COMMENT ON COLUMN public.ucp_connections.config_updated_at IS 'Timestamp of the last configuration change, used by the Tool Registry cache fingerprint. Operational columns (last_used_at, last_error) do not touch it.';

CREATE OR REPLACE FUNCTION public.ucp_connections_touch_config_updated_at()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public
AS $$
BEGIN
    IF (
        OLD.is_active IS DISTINCT FROM NEW.is_active
        OR OLD.store_url IS DISTINCT FROM NEW.store_url
        OR OLD.manifest_version IS DISTINCT FROM NEW.manifest_version
        OR OLD.preferred_transport IS DISTINCT FROM NEW.preferred_transport
        OR OLD.capabilities_enabled IS DISTINCT FROM NEW.capabilities_enabled
    ) THEN
        NEW.config_updated_at = clock_timestamp();
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS ucp_connections_touch_config ON public.ucp_connections;
CREATE TRIGGER ucp_connections_touch_config
BEFORE UPDATE ON public.ucp_connections
FOR EACH ROW
EXECUTE FUNCTION public.ucp_connections_touch_config_updated_at();

COMMIT;
