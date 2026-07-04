-- Cache fingerprint - add config_updated_at to agent_mcp_connections.
--
-- Context:
-- agent_mcp_connections rows are updated very frequently by the OAuth refresh flow
-- (access_token, refresh_token, token_expires_at). Those token rotations must NOT
-- invalidate the Tool Registry cache. Only structural/config changes do:
-- is_active and mcp_server_id.
--
-- A dedicated config_updated_at column plus a conditional BEFORE UPDATE trigger
-- isolates the fingerprint from token churn. clock_timestamp() is used (instead of
-- now()) so the value advances even within a single transaction.
--
-- Idempotency:
-- ADD COLUMN IF NOT EXISTS + CREATE OR REPLACE FUNCTION + DROP TRIGGER IF EXISTS
-- guarantee that re-running the migration does not raise.

BEGIN;

ALTER TABLE public.agent_mcp_connections
    ADD COLUMN IF NOT EXISTS config_updated_at timestamptz NOT NULL DEFAULT now();

-- Defensive constraint enforcement for partial/older deploys.
ALTER TABLE public.agent_mcp_connections
    ALTER COLUMN config_updated_at SET DEFAULT now();

UPDATE public.agent_mcp_connections
SET config_updated_at = now()
WHERE config_updated_at IS NULL;

ALTER TABLE public.agent_mcp_connections
    ALTER COLUMN config_updated_at SET NOT NULL;

COMMENT ON COLUMN public.agent_mcp_connections.config_updated_at IS 'Timestamp of the last configuration change, used by the Tool Registry cache fingerprint. OAuth token columns (access_token, refresh_token, token_expires_at) do not touch it.';

CREATE OR REPLACE FUNCTION public.agent_mcp_connections_touch_config_updated_at()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public
AS $$
BEGIN
    IF (
        OLD.is_active IS DISTINCT FROM NEW.is_active
        OR OLD.mcp_server_id IS DISTINCT FROM NEW.mcp_server_id
    ) THEN
        NEW.config_updated_at = clock_timestamp();
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS agent_mcp_connections_touch_config ON public.agent_mcp_connections;
CREATE TRIGGER agent_mcp_connections_touch_config
BEFORE UPDATE ON public.agent_mcp_connections
FOR EACH ROW
EXECUTE FUNCTION public.agent_mcp_connections_touch_config_updated_at();

COMMIT;
