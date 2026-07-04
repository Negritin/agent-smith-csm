-- Cache fingerprint - add updated_at to agent_mcp_tools.
--
-- Context:
-- The new Tool Registry runtime builds a cache fingerprint from row timestamps.
-- agent_mcp_tools only had created_at, so updates were invisible to the cache.
-- This migration adds updated_at plus a BEFORE UPDATE trigger that keeps it fresh.
--
-- Idempotency:
-- ADD COLUMN IF NOT EXISTS + DROP TRIGGER IF EXISTS guarantee that re-running the
-- migration (or applying it twice in a row) does not raise.

BEGIN;

ALTER TABLE public.agent_mcp_tools
    ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

-- Defensive backfill/constraint enforcement when the column already existed
-- without the expected DEFAULT/NOT NULL (older partial deploys).
ALTER TABLE public.agent_mcp_tools
    ALTER COLUMN updated_at SET DEFAULT now();

UPDATE public.agent_mcp_tools
SET updated_at = now()
WHERE updated_at IS NULL;

ALTER TABLE public.agent_mcp_tools
    ALTER COLUMN updated_at SET NOT NULL;

COMMENT ON COLUMN public.agent_mcp_tools.updated_at IS 'Last mutation timestamp, used by the Tool Registry cache fingerprint.';

-- Reuse the shared updated_at helper (defined in schema_completo.sql).
DROP TRIGGER IF EXISTS update_agent_mcp_tools_updated_at ON public.agent_mcp_tools;
CREATE TRIGGER update_agent_mcp_tools_updated_at
BEFORE UPDATE ON public.agent_mcp_tools
FOR EACH ROW
EXECUTE FUNCTION public.update_updated_at_column();

COMMIT;
