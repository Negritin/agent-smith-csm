-- Sprint 4 - Banco, migrations e configuracao
-- Hardens tenant RLS, removes dangerous anon grants, and makes admin_users.role explicit.

BEGIN;

-- admin_users stores master admin credentials; company admins authenticate from users_v2.
-- Make legacy rows explicit without changing the login table contract.
ALTER TABLE public.admin_users
    ADD COLUMN IF NOT EXISTS role text,
    ADD COLUMN IF NOT EXISTS company_id uuid;

UPDATE public.admin_users
SET role = 'master_admin'
WHERE role IS NULL OR btrim(role) = '';

ALTER TABLE public.admin_users
    ALTER COLUMN role SET DEFAULT 'master_admin',
    ALTER COLUMN role SET NOT NULL;

COMMENT ON COLUMN public.admin_users.role IS 'Security role for admin sessions. admin_users stores master admins; company admins authenticate from users_v2.';
COMMENT ON COLUMN public.admin_users.company_id IS 'Tenant scope only for explicitly tenant-scoped legacy admin_users records; master_admin rows keep this null.';

-- Some security-sensitive child tables did not have a direct tenant column in the dump.
-- Backfill the new company_id from the owning agent where that relationship exists.
ALTER TABLE public.agent_delegations
    ADD COLUMN IF NOT EXISTS company_id uuid;

UPDATE public.agent_delegations AS delegation
SET company_id = orchestrator.company_id
FROM public.agents AS orchestrator
WHERE delegation.orchestrator_id = orchestrator.id
  AND delegation.company_id IS NULL;

ALTER TABLE public.agent_http_tools
    ADD COLUMN IF NOT EXISTS company_id uuid;

UPDATE public.agent_http_tools AS tool
SET company_id = agent.company_id
FROM public.agents AS agent
WHERE tool.agent_id = agent.id
  AND tool.company_id IS NULL;

ALTER TABLE public.agent_mcp_connections
    ADD COLUMN IF NOT EXISTS company_id uuid;

UPDATE public.agent_mcp_connections AS connection
SET company_id = agent.company_id
FROM public.agents AS agent
WHERE connection.agent_id = agent.id
  AND connection.company_id IS NULL;

ALTER TABLE public.agent_mcp_tools
    ADD COLUMN IF NOT EXISTS company_id uuid;

UPDATE public.agent_mcp_tools AS tool
SET company_id = agent.company_id
FROM public.agents AS agent
WHERE tool.agent_id = agent.id
  AND tool.company_id IS NULL;

ALTER TABLE public.checkpoints
    ADD COLUMN IF NOT EXISTS company_id uuid;

COMMENT ON COLUMN public.agent_delegations.company_id IS 'Tenant scope for delegation RLS; backfilled from orchestrator agent.';
COMMENT ON COLUMN public.agent_http_tools.company_id IS 'Tenant scope for HTTP tool RLS; backfilled from the owning agent.';
COMMENT ON COLUMN public.agent_mcp_connections.company_id IS 'Tenant scope for MCP connection RLS; backfilled from the owning agent.';
COMMENT ON COLUMN public.agent_mcp_tools.company_id IS 'Tenant scope for MCP tool RLS; backfilled from the owning agent.';
COMMENT ON COLUMN public.checkpoints.company_id IS 'Tenant scope for LangGraph checkpoint RLS; nullable for legacy rows until application backfill.';

CREATE INDEX IF NOT EXISTS idx_admin_users_company_id ON public.admin_users(company_id);
CREATE INDEX IF NOT EXISTS idx_agent_delegations_company_id ON public.agent_delegations(company_id);
CREATE INDEX IF NOT EXISTS idx_agent_http_tools_company_id ON public.agent_http_tools(company_id);
CREATE INDEX IF NOT EXISTS idx_agent_mcp_connections_company_id ON public.agent_mcp_connections(company_id);
CREATE INDEX IF NOT EXISTS idx_agent_mcp_tools_company_id ON public.agent_mcp_tools(company_id);
CREATE INDEX IF NOT EXISTS idx_checkpoints_company_id ON public.checkpoints(company_id);

-- ALTO-005: remove broad public DML on sensitive operational tables.
REVOKE ALL PRIVILEGES ON TABLE public.agent_delegations FROM anon;
REVOKE ALL PRIVILEGES ON TABLE public.sanitization_jobs FROM anon;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.agent_delegations TO authenticated;

-- Limit historical GRANT ALL entries to the DML operations covered by tenant policies.
REVOKE ALL PRIVILEGES ON TABLE
    public.agents,
    public.documents,
    public.conversation_logs,
    public.agent_http_tools,
    public.agent_mcp_connections,
    public.agent_mcp_tools,
    public.system_logs,
    public.checkpoints,
    public.memory_processing_locks,
    public.companies,
    public.users_v2,
    public.admin_users
FROM authenticated, anon;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    public.agents,
    public.documents,
    public.conversation_logs,
    public.agent_http_tools,
    public.agent_mcp_connections,
    public.agent_mcp_tools,
    public.system_logs,
    public.checkpoints,
    public.memory_processing_locks,
    public.companies,
    public.users_v2,
    public.admin_users
TO authenticated, anon;

ALTER TABLE public.agent_delegations ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS delegations_same_company ON public.agent_delegations;
CREATE POLICY delegations_same_company ON public.agent_delegations
    FOR ALL
    TO authenticated
    USING (
        company_id = (auth.jwt() ->> 'company_id')::uuid
        AND EXISTS (
            SELECT 1
            FROM public.agents AS orchestrator
            JOIN public.agents AS subagent
              ON subagent.id = agent_delegations.subagent_id
            WHERE orchestrator.id = agent_delegations.orchestrator_id
              AND orchestrator.company_id = agent_delegations.company_id
              AND subagent.company_id = agent_delegations.company_id
        )
    )
    WITH CHECK (
        company_id = (auth.jwt() ->> 'company_id')::uuid
        AND EXISTS (
            SELECT 1
            FROM public.agents AS orchestrator
            JOIN public.agents AS subagent
              ON subagent.id = agent_delegations.subagent_id
            WHERE orchestrator.id = agent_delegations.orchestrator_id
              AND orchestrator.company_id = agent_delegations.company_id
              AND subagent.company_id = agent_delegations.company_id
        )
    );

-- ALTO-006 / T12: tenant-scoped RLS for authenticated and anon roles.
ALTER TABLE public.agents ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.conversation_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_http_tools ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_mcp_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_mcp_tools ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.system_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.memory_processing_locks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.companies ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.users_v2 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.admin_users ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_select_agents ON public.agents;
CREATE POLICY tenant_select_agents ON public.agents
    FOR SELECT TO authenticated, anon
    USING (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_insert_agents ON public.agents;
CREATE POLICY tenant_insert_agents ON public.agents
    FOR INSERT TO authenticated, anon
    WITH CHECK (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_update_agents ON public.agents;
CREATE POLICY tenant_update_agents ON public.agents
    FOR UPDATE TO authenticated, anon
    USING (company_id = (auth.jwt() ->> 'company_id')::uuid)
    WITH CHECK (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_delete_agents ON public.agents;
CREATE POLICY tenant_delete_agents ON public.agents
    FOR DELETE TO authenticated, anon
    USING (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_select_documents ON public.documents;
CREATE POLICY tenant_select_documents ON public.documents
    FOR SELECT TO authenticated, anon
    USING (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_insert_documents ON public.documents;
CREATE POLICY tenant_insert_documents ON public.documents
    FOR INSERT TO authenticated, anon
    WITH CHECK (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_update_documents ON public.documents;
CREATE POLICY tenant_update_documents ON public.documents
    FOR UPDATE TO authenticated, anon
    USING (company_id = (auth.jwt() ->> 'company_id')::uuid)
    WITH CHECK (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_delete_documents ON public.documents;
CREATE POLICY tenant_delete_documents ON public.documents
    FOR DELETE TO authenticated, anon
    USING (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_select_conversation_logs ON public.conversation_logs;
CREATE POLICY tenant_select_conversation_logs ON public.conversation_logs
    FOR SELECT TO authenticated, anon
    USING (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_insert_conversation_logs ON public.conversation_logs;
CREATE POLICY tenant_insert_conversation_logs ON public.conversation_logs
    FOR INSERT TO authenticated, anon
    WITH CHECK (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_update_conversation_logs ON public.conversation_logs;
CREATE POLICY tenant_update_conversation_logs ON public.conversation_logs
    FOR UPDATE TO authenticated, anon
    USING (company_id = (auth.jwt() ->> 'company_id')::uuid)
    WITH CHECK (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_delete_conversation_logs ON public.conversation_logs;
CREATE POLICY tenant_delete_conversation_logs ON public.conversation_logs
    FOR DELETE TO authenticated, anon
    USING (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_select_agent_http_tools ON public.agent_http_tools;
CREATE POLICY tenant_select_agent_http_tools ON public.agent_http_tools
    FOR SELECT TO authenticated, anon
    USING (
        company_id = (auth.jwt() ->> 'company_id')::uuid
        AND EXISTS (
            SELECT 1 FROM public.agents
            WHERE agents.id = agent_http_tools.agent_id
              AND agents.company_id = agent_http_tools.company_id
        )
    );

DROP POLICY IF EXISTS tenant_insert_agent_http_tools ON public.agent_http_tools;
CREATE POLICY tenant_insert_agent_http_tools ON public.agent_http_tools
    FOR INSERT TO authenticated, anon
    WITH CHECK (
        company_id = (auth.jwt() ->> 'company_id')::uuid
        AND EXISTS (
            SELECT 1 FROM public.agents
            WHERE agents.id = agent_http_tools.agent_id
              AND agents.company_id = agent_http_tools.company_id
        )
    );

DROP POLICY IF EXISTS tenant_update_agent_http_tools ON public.agent_http_tools;
CREATE POLICY tenant_update_agent_http_tools ON public.agent_http_tools
    FOR UPDATE TO authenticated, anon
    USING (
        company_id = (auth.jwt() ->> 'company_id')::uuid
        AND EXISTS (
            SELECT 1 FROM public.agents
            WHERE agents.id = agent_http_tools.agent_id
              AND agents.company_id = agent_http_tools.company_id
        )
    )
    WITH CHECK (
        company_id = (auth.jwt() ->> 'company_id')::uuid
        AND EXISTS (
            SELECT 1 FROM public.agents
            WHERE agents.id = agent_http_tools.agent_id
              AND agents.company_id = agent_http_tools.company_id
        )
    );

DROP POLICY IF EXISTS tenant_delete_agent_http_tools ON public.agent_http_tools;
CREATE POLICY tenant_delete_agent_http_tools ON public.agent_http_tools
    FOR DELETE TO authenticated, anon
    USING (
        company_id = (auth.jwt() ->> 'company_id')::uuid
        AND EXISTS (
            SELECT 1 FROM public.agents
            WHERE agents.id = agent_http_tools.agent_id
              AND agents.company_id = agent_http_tools.company_id
        )
    );

DROP POLICY IF EXISTS tenant_select_agent_mcp_connections ON public.agent_mcp_connections;
CREATE POLICY tenant_select_agent_mcp_connections ON public.agent_mcp_connections
    FOR SELECT TO authenticated, anon
    USING (
        company_id = (auth.jwt() ->> 'company_id')::uuid
        AND EXISTS (
            SELECT 1 FROM public.agents
            WHERE agents.id = agent_mcp_connections.agent_id
              AND agents.company_id = agent_mcp_connections.company_id
        )
    );

DROP POLICY IF EXISTS tenant_insert_agent_mcp_connections ON public.agent_mcp_connections;
CREATE POLICY tenant_insert_agent_mcp_connections ON public.agent_mcp_connections
    FOR INSERT TO authenticated, anon
    WITH CHECK (
        company_id = (auth.jwt() ->> 'company_id')::uuid
        AND EXISTS (
            SELECT 1 FROM public.agents
            WHERE agents.id = agent_mcp_connections.agent_id
              AND agents.company_id = agent_mcp_connections.company_id
        )
    );

DROP POLICY IF EXISTS tenant_update_agent_mcp_connections ON public.agent_mcp_connections;
CREATE POLICY tenant_update_agent_mcp_connections ON public.agent_mcp_connections
    FOR UPDATE TO authenticated, anon
    USING (
        company_id = (auth.jwt() ->> 'company_id')::uuid
        AND EXISTS (
            SELECT 1 FROM public.agents
            WHERE agents.id = agent_mcp_connections.agent_id
              AND agents.company_id = agent_mcp_connections.company_id
        )
    )
    WITH CHECK (
        company_id = (auth.jwt() ->> 'company_id')::uuid
        AND EXISTS (
            SELECT 1 FROM public.agents
            WHERE agents.id = agent_mcp_connections.agent_id
              AND agents.company_id = agent_mcp_connections.company_id
        )
    );

DROP POLICY IF EXISTS tenant_delete_agent_mcp_connections ON public.agent_mcp_connections;
CREATE POLICY tenant_delete_agent_mcp_connections ON public.agent_mcp_connections
    FOR DELETE TO authenticated, anon
    USING (
        company_id = (auth.jwt() ->> 'company_id')::uuid
        AND EXISTS (
            SELECT 1 FROM public.agents
            WHERE agents.id = agent_mcp_connections.agent_id
              AND agents.company_id = agent_mcp_connections.company_id
        )
    );

DROP POLICY IF EXISTS tenant_select_agent_mcp_tools ON public.agent_mcp_tools;
CREATE POLICY tenant_select_agent_mcp_tools ON public.agent_mcp_tools
    FOR SELECT TO authenticated, anon
    USING (
        company_id = (auth.jwt() ->> 'company_id')::uuid
        AND EXISTS (
            SELECT 1 FROM public.agents
            WHERE agents.id = agent_mcp_tools.agent_id
              AND agents.company_id = agent_mcp_tools.company_id
        )
    );

DROP POLICY IF EXISTS tenant_insert_agent_mcp_tools ON public.agent_mcp_tools;
CREATE POLICY tenant_insert_agent_mcp_tools ON public.agent_mcp_tools
    FOR INSERT TO authenticated, anon
    WITH CHECK (
        company_id = (auth.jwt() ->> 'company_id')::uuid
        AND EXISTS (
            SELECT 1 FROM public.agents
            WHERE agents.id = agent_mcp_tools.agent_id
              AND agents.company_id = agent_mcp_tools.company_id
        )
    );

DROP POLICY IF EXISTS tenant_update_agent_mcp_tools ON public.agent_mcp_tools;
CREATE POLICY tenant_update_agent_mcp_tools ON public.agent_mcp_tools
    FOR UPDATE TO authenticated, anon
    USING (
        company_id = (auth.jwt() ->> 'company_id')::uuid
        AND EXISTS (
            SELECT 1 FROM public.agents
            WHERE agents.id = agent_mcp_tools.agent_id
              AND agents.company_id = agent_mcp_tools.company_id
        )
    )
    WITH CHECK (
        company_id = (auth.jwt() ->> 'company_id')::uuid
        AND EXISTS (
            SELECT 1 FROM public.agents
            WHERE agents.id = agent_mcp_tools.agent_id
              AND agents.company_id = agent_mcp_tools.company_id
        )
    );

DROP POLICY IF EXISTS tenant_delete_agent_mcp_tools ON public.agent_mcp_tools;
CREATE POLICY tenant_delete_agent_mcp_tools ON public.agent_mcp_tools
    FOR DELETE TO authenticated, anon
    USING (
        company_id = (auth.jwt() ->> 'company_id')::uuid
        AND EXISTS (
            SELECT 1 FROM public.agents
            WHERE agents.id = agent_mcp_tools.agent_id
              AND agents.company_id = agent_mcp_tools.company_id
        )
    );

DROP POLICY IF EXISTS tenant_select_system_logs ON public.system_logs;
CREATE POLICY tenant_select_system_logs ON public.system_logs
    FOR SELECT TO authenticated, anon
    USING (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_insert_system_logs ON public.system_logs;
CREATE POLICY tenant_insert_system_logs ON public.system_logs
    FOR INSERT TO authenticated, anon
    WITH CHECK (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_update_system_logs ON public.system_logs;
CREATE POLICY tenant_update_system_logs ON public.system_logs
    FOR UPDATE TO authenticated, anon
    USING (company_id = (auth.jwt() ->> 'company_id')::uuid)
    WITH CHECK (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_delete_system_logs ON public.system_logs;
CREATE POLICY tenant_delete_system_logs ON public.system_logs
    FOR DELETE TO authenticated, anon
    USING (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_select_checkpoints ON public.checkpoints;
CREATE POLICY tenant_select_checkpoints ON public.checkpoints
    FOR SELECT TO authenticated, anon
    USING (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_insert_checkpoints ON public.checkpoints;
CREATE POLICY tenant_insert_checkpoints ON public.checkpoints
    FOR INSERT TO authenticated, anon
    WITH CHECK (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_update_checkpoints ON public.checkpoints;
CREATE POLICY tenant_update_checkpoints ON public.checkpoints
    FOR UPDATE TO authenticated, anon
    USING (company_id = (auth.jwt() ->> 'company_id')::uuid)
    WITH CHECK (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_delete_checkpoints ON public.checkpoints;
CREATE POLICY tenant_delete_checkpoints ON public.checkpoints
    FOR DELETE TO authenticated, anon
    USING (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_select_memory_processing_locks ON public.memory_processing_locks;
CREATE POLICY tenant_select_memory_processing_locks ON public.memory_processing_locks
    FOR SELECT TO authenticated, anon
    USING (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_insert_memory_processing_locks ON public.memory_processing_locks;
CREATE POLICY tenant_insert_memory_processing_locks ON public.memory_processing_locks
    FOR INSERT TO authenticated, anon
    WITH CHECK (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_update_memory_processing_locks ON public.memory_processing_locks;
CREATE POLICY tenant_update_memory_processing_locks ON public.memory_processing_locks
    FOR UPDATE TO authenticated, anon
    USING (company_id = (auth.jwt() ->> 'company_id')::uuid)
    WITH CHECK (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_delete_memory_processing_locks ON public.memory_processing_locks;
CREATE POLICY tenant_delete_memory_processing_locks ON public.memory_processing_locks
    FOR DELETE TO authenticated, anon
    USING (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_select_companies ON public.companies;
CREATE POLICY tenant_select_companies ON public.companies
    FOR SELECT TO authenticated, anon
    USING (id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_insert_companies ON public.companies;
CREATE POLICY tenant_insert_companies ON public.companies
    FOR INSERT TO authenticated, anon
    WITH CHECK (id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_update_companies ON public.companies;
CREATE POLICY tenant_update_companies ON public.companies
    FOR UPDATE TO authenticated, anon
    USING (id = (auth.jwt() ->> 'company_id')::uuid)
    WITH CHECK (id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_delete_companies ON public.companies;
CREATE POLICY tenant_delete_companies ON public.companies
    FOR DELETE TO authenticated, anon
    USING (id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_select_users_v2 ON public.users_v2;
CREATE POLICY tenant_select_users_v2 ON public.users_v2
    FOR SELECT TO authenticated, anon
    USING (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_insert_users_v2 ON public.users_v2;
CREATE POLICY tenant_insert_users_v2 ON public.users_v2
    FOR INSERT TO authenticated, anon
    WITH CHECK (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_update_users_v2 ON public.users_v2;
CREATE POLICY tenant_update_users_v2 ON public.users_v2
    FOR UPDATE TO authenticated, anon
    USING (company_id = (auth.jwt() ->> 'company_id')::uuid)
    WITH CHECK (company_id = (auth.jwt() ->> 'company_id')::uuid);

DROP POLICY IF EXISTS tenant_delete_users_v2 ON public.users_v2;
CREATE POLICY tenant_delete_users_v2 ON public.users_v2
    FOR DELETE TO authenticated, anon
    USING (company_id = (auth.jwt() ->> 'company_id')::uuid);

-- admin_users is intentionally narrower: row role alone never grants access.
-- The JWT role claim must be master_admin, or a company_admin claim must match company_id.
DROP POLICY IF EXISTS tenant_select_admin_users ON public.admin_users;
CREATE POLICY tenant_select_admin_users ON public.admin_users
    FOR SELECT TO authenticated, anon
    USING (
        (auth.jwt() ->> 'role') = 'master_admin'
        OR (
            (auth.jwt() ->> 'role') = 'company_admin'
            AND role = 'company_admin'
            AND company_id = (auth.jwt() ->> 'company_id')::uuid
        )
    );

DROP POLICY IF EXISTS tenant_insert_admin_users ON public.admin_users;
CREATE POLICY tenant_insert_admin_users ON public.admin_users
    FOR INSERT TO authenticated, anon
    WITH CHECK (
        (auth.jwt() ->> 'role') = 'master_admin'
        OR (
            (auth.jwt() ->> 'role') = 'company_admin'
            AND role = 'company_admin'
            AND company_id = (auth.jwt() ->> 'company_id')::uuid
        )
    );

DROP POLICY IF EXISTS tenant_update_admin_users ON public.admin_users;
CREATE POLICY tenant_update_admin_users ON public.admin_users
    FOR UPDATE TO authenticated, anon
    USING (
        (auth.jwt() ->> 'role') = 'master_admin'
        OR (
            (auth.jwt() ->> 'role') = 'company_admin'
            AND role = 'company_admin'
            AND company_id = (auth.jwt() ->> 'company_id')::uuid
        )
    )
    WITH CHECK (
        (auth.jwt() ->> 'role') = 'master_admin'
        OR (
            (auth.jwt() ->> 'role') = 'company_admin'
            AND role = 'company_admin'
            AND company_id = (auth.jwt() ->> 'company_id')::uuid
        )
    );

DROP POLICY IF EXISTS tenant_delete_admin_users ON public.admin_users;
CREATE POLICY tenant_delete_admin_users ON public.admin_users
    FOR DELETE TO authenticated, anon
    USING (
        (auth.jwt() ->> 'role') = 'master_admin'
        OR (
            (auth.jwt() ->> 'role') = 'company_admin'
            AND role = 'company_admin'
            AND company_id = (auth.jwt() ->> 'company_id')::uuid
        )
    );

COMMIT;
