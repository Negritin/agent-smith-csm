-- MCPs oficiais remotos - fundacao de banco (SPEC impl 2026-06-12 §1).
--
-- Context:
-- 1. mcp_servers ganha server_type ('internal' | 'remote'), url e extra_headers
--    para o catalogo dos servers remotos (Notion, Klaviyo, Sentry, Supabase,
--    Higgsfield). url e obrigatoria quando server_type = 'remote'.
-- 2. mcp_oauth_clients (nova): client registration DCR (RFC 7591) da PLATAFORMA,
--    1 por servidor remoto. client_secret/registration_access_token sao gravados
--    criptografados pelo backend (encryption_service). Sem RLS nova: acessada
--    apenas via service role, mesmo padrao de mcp_servers.
-- 3. agent_mcp_connections ganha connection_config (ex.: {"project_ref": "..."}
--    no Supabase) e connection_metadata (identidade NAO sensivel da conta /
--    workspace conectada — ex.: workspace_name/workspace_id do Notion — consumida
--    pela UI da secao Ferramentas).
-- 4. agent_mcp_tools ganha is_available: tool que sumiu do tools/list do servidor
--    fica indisponivel, NAO deletada (preserva a curadoria de is_enabled).
-- 5. agent_mcp_connections_touch_config_updated_at e estendida para tambem
--    avancar config_updated_at quando connection_config muda (opcao (a) da SPEC):
--    config_updated_at e fonte do fingerprint do Tool Registry, e mudar p.ex. o
--    project_ref do Supabase precisa invalidar o snapshot. O PATCH de config NAO
--    toca config_updated_at manualmente — o trigger cobre.
--
-- Idempotency:
-- ADD COLUMN IF NOT EXISTS, CREATE TABLE IF NOT EXISTS, DO-block para os CHECKs,
-- CREATE OR REPLACE FUNCTION e DROP TRIGGER IF EXISTS garantem que re-rodar a
-- migration nao levanta.

BEGIN;

-- =========================================================================
-- 1. Catalogo: tipo de servidor + endpoint remoto
-- =========================================================================
ALTER TABLE public.mcp_servers
    ADD COLUMN IF NOT EXISTS server_type varchar(20) NOT NULL DEFAULT 'internal',
    ADD COLUMN IF NOT EXISTS url text,
    ADD COLUMN IF NOT EXISTS extra_headers jsonb DEFAULT '{}'::jsonb;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'mcp_servers_server_type_check'
          AND conrelid = 'public.mcp_servers'::regclass
    ) THEN
        ALTER TABLE public.mcp_servers
            ADD CONSTRAINT mcp_servers_server_type_check
            CHECK (server_type IN ('internal', 'remote'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'mcp_servers_remote_url_check'
          AND conrelid = 'public.mcp_servers'::regclass
    ) THEN
        ALTER TABLE public.mcp_servers
            ADD CONSTRAINT mcp_servers_remote_url_check
            CHECK (server_type <> 'remote' OR url IS NOT NULL);
    END IF;
END;
$$;

COMMENT ON COLUMN public.mcp_servers.server_type IS 'internal = subprocess stdio (SUP-MCP-020) | remote = Streamable HTTP oficial do provider.';
COMMENT ON COLUMN public.mcp_servers.url IS 'Endpoint do MCP remoto (https obrigatorio; validado tambem no backend). NULL para internos.';
COMMENT ON COLUMN public.mcp_servers.extra_headers IS 'Headers fixos adicionais enviados em toda chamada ao servidor remoto.';

-- =========================================================================
-- 2. Client registrations DCR (da PLATAFORMA, 1 por servidor remoto)
-- =========================================================================
CREATE TABLE IF NOT EXISTS public.mcp_oauth_clients (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    mcp_server_id uuid NOT NULL UNIQUE REFERENCES public.mcp_servers(id) ON DELETE CASCADE,
    client_id text NOT NULL,
    client_secret text,                       -- criptografado; null = public client
    registration_access_token text,           -- criptografado; null se provider nao retornar
    registration_client_uri text,
    auth_metadata jsonb DEFAULT '{}'::jsonb,  -- cache RFC 8414 (authorization/token endpoints, issuer)
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now()
);

COMMENT ON TABLE public.mcp_oauth_clients IS 'Registro OAuth (DCR RFC 7591) do Agent Smith junto a cada MCP server remoto. Secrets criptografados pelo backend (encryption_service). Acesso apenas via service role.';

-- Convencao do projeto: updated_at mantido pelo helper compartilhado
-- (ver 20260528_add_updated_at_to_agent_mcp_tools.sql / schema_completo.sql).
DROP TRIGGER IF EXISTS update_mcp_oauth_clients_updated_at ON public.mcp_oauth_clients;
CREATE TRIGGER update_mcp_oauth_clients_updated_at
BEFORE UPDATE ON public.mcp_oauth_clients
FOR EACH ROW
EXECUTE FUNCTION public.update_updated_at_column();

-- =========================================================================
-- 3. Config + identidade por conexao (agent_id, mcp_server_id)
-- =========================================================================
ALTER TABLE public.agent_mcp_connections
    ADD COLUMN IF NOT EXISTS connection_config jsonb DEFAULT '{}'::jsonb;

ALTER TABLE public.agent_mcp_connections
    ADD COLUMN IF NOT EXISTS connection_metadata jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN public.agent_mcp_connections.connection_config IS 'Config por conexao consumida na montagem da URL/chamada remota (ex.: {"project_ref": "abc"} no Supabase). Mudanca avanca config_updated_at via trigger.';
COMMENT ON COLUMN public.agent_mcp_connections.connection_metadata IS 'Identidade NAO sensivel da conta/workspace conectada (ex.: workspace_name/workspace_id do Notion), exibida na UI. Nunca tokens.';

-- =========================================================================
-- 4. Drift de tools: sumiu do servidor != deletada (preserva curadoria)
-- =========================================================================
ALTER TABLE public.agent_mcp_tools
    ADD COLUMN IF NOT EXISTS is_available boolean NOT NULL DEFAULT true;

COMMENT ON COLUMN public.agent_mcp_tools.is_available IS 'false = tool ausente do ultimo tools/list do servidor. Nao deletamos para preservar a curadoria (is_enabled).';

-- =========================================================================
-- 5. Fingerprint: connection_config tambem avanca config_updated_at
-- =========================================================================
-- Estende a funcao definida em
-- 20260528_add_config_updated_at_to_agent_mcp_connections.sql: alem de
-- is_active e mcp_server_id, connection_config passa a tocar
-- config_updated_at (fonte do fingerprint do registry). Token churn
-- (access_token/refresh_token/token_expires_at) continua NAO invalidando.
CREATE OR REPLACE FUNCTION public.agent_mcp_connections_touch_config_updated_at()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public
AS $$
BEGIN
    IF (
        OLD.is_active IS DISTINCT FROM NEW.is_active
        OR OLD.mcp_server_id IS DISTINCT FROM NEW.mcp_server_id
        OR OLD.connection_config IS DISTINCT FROM NEW.connection_config
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
