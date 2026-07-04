-- Cache coherence - add the missing BEFORE UPDATE trigger to public.agents (F11).
--
-- Context:
--   Both the graph cache key (compute_graph_cache_key, app/services/graph_cache.py)
--   and the Tool Registry fingerprint (app/agents/runtime/registry.py) are derived
--   from agents.updated_at. The agents table HAS the updated_at column
--   (schema_completo.sql, DEFAULT now()) but never had a BEFORE UPDATE trigger, so
--   updated_at only changed on INSERT. AgentService.update_agent() also never sets
--   it manually. With a single worker this was masked by in-process cache
--   invalidation; once scaled horizontally (multiple workers/replicas) an admin
--   config edit invalidated only ONE process's cache while the others kept serving
--   the stale model/system-prompt/tools indefinitely.
--
--   This migration adds a BEFORE UPDATE trigger reusing the shared
--   public.update_updated_at_column() (defined in schema_completo.sql), exactly
--   mirroring 20260528_add_updated_at_to_agent_mcp_tools.sql. Now every write to
--   agents bumps updated_at = now(), so the cache key / fingerprint change
--   automatically and stale entries are never reused across replicas — without any
--   cross-process invalidation.
--
-- Idempotency:
--   DROP TRIGGER IF EXISTS guarantees that re-running the migration (or applying it
--   twice in a row) does not raise. The updated_at column already exists, so no
--   ADD COLUMN is needed.

BEGIN;

-- Reuse the shared updated_at helper (defined in schema_completo.sql).
DROP TRIGGER IF EXISTS update_agents_updated_at ON public.agents;
CREATE TRIGGER update_agents_updated_at
BEFORE UPDATE ON public.agents
FOR EACH ROW
EXECUTE FUNCTION public.update_updated_at_column();

COMMIT;
