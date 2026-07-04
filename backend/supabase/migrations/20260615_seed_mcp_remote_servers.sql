-- Seed via SQL dos 5 MCP servers REMOTOS oficiais (Notion, Klaviyo, Sentry,
-- Supabase, Higgsfield). Equivalente ao scripts/seed_mcp_servers.py, porem
-- executavel direto no banco de producao quando nao ha conexao local
-- (so migration disponivel).
--
-- Depende da migration 20260612_mcp_remote_servers.sql (colunas server_type,
-- url, extra_headers + CHECKs). Rode-a antes desta.
--
-- Idempotente: ON CONFLICT (name) DO UPDATE. Re-rodar nao duplica e
-- re-sincroniza display_name/description/url.
--
-- ATIVACAO (is_active=true): este seed ja ATIVA os 5 remotos. O design original
-- gateava a ativacao atras do spike da Fase 0 (scripts/spike_remote_mcp.py:
-- valida OAuth 2.1 + DCR + tools/list por provider). O spike NAO precisa de
-- conexao com o banco (so browser + conta real no provider) — recomendado rodar
-- antes. Ressalvas conhecidas: Higgsfield (DCR RFC 7591 ainda nao confirmado em
-- producao) e Notion (rotacao de refresh token). Se um provider falhar ao
-- conectar na UI, desative-o pontualmente:
--     UPDATE public.mcp_servers SET is_active = false
--      WHERE name = 'higgsfield' AND server_type = 'remote';

BEGIN;

INSERT INTO public.mcp_servers
    (name, display_name, description, server_type, url, oauth_provider, package_name, command, is_active)
VALUES
    ('notion',     'Notion',     'Paginas, bancos de dados e busca no workspace Notion',          'remote', 'https://mcp.notion.com/mcp',     'notion',     'remote', '[]'::jsonb, true),
    ('klaviyo',    'Klaviyo',    'Campanhas, listas e metricas de marketing do Klaviyo',          'remote', 'https://mcp.klaviyo.com/mcp',    'klaviyo',    'remote', '[]'::jsonb, true),
    ('sentry',     'Sentry',     'Issues, eventos e projetos de observabilidade do Sentry',       'remote', 'https://mcp.sentry.dev/mcp',     'sentry',     'remote', '[]'::jsonb, true),
    ('supabase',   'Supabase',   'Projetos e SQL do Supabase (requer project_ref na conexao)',    'remote', 'https://mcp.supabase.com/mcp',   'supabase',   'remote', '[]'::jsonb, true),
    ('higgsfield', 'Higgsfield', 'Geracao de imagens e videos com IA via Higgsfield',             'remote', 'https://mcp.higgsfield.ai/mcp',  'higgsfield', 'remote', '[]'::jsonb, true)
ON CONFLICT (name) DO UPDATE SET
    display_name  = EXCLUDED.display_name,
    description   = EXCLUDED.description,
    server_type   = EXCLUDED.server_type,
    url           = EXCLUDED.url,
    oauth_provider= EXCLUDED.oauth_provider,
    package_name  = EXCLUDED.package_name,
    command       = EXCLUDED.command,
    is_active     = EXCLUDED.is_active,
    updated_at    = now();

COMMIT;
