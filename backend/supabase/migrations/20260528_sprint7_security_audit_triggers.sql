-- Sprint 7 - Security audit triggers for sensitive database mutations.

BEGIN;

CREATE OR REPLACE FUNCTION public.write_security_audit_log(
    p_action text,
    p_company_id uuid,
    p_resource_type text,
    p_resource_id uuid,
    p_status text DEFAULT 'success',
    p_details jsonb DEFAULT '{}'::jsonb
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    INSERT INTO public.system_logs (
        company_id,
        action_type,
        resource_type,
        resource_id,
        status,
        details
    ) VALUES (
        p_company_id,
        p_action,
        p_resource_type,
        p_resource_id,
        p_status,
        COALESCE(p_details, '{}'::jsonb) || jsonb_build_object(
            'category', 'security_audit',
            'action', p_action,
            'targetId', p_resource_id,
            'targetCompanyId', p_company_id,
            'source', 'db_trigger'
        )
    );
END;
$$;

-- SECURITY DEFINER helper must not be callable as public RPC.
REVOKE ALL ON FUNCTION public.write_security_audit_log(text, uuid, text, uuid, text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.write_security_audit_log(text, uuid, text, uuid, text, jsonb) FROM anon;
REVOKE ALL ON FUNCTION public.write_security_audit_log(text, uuid, text, uuid, text, jsonb) FROM authenticated;

CREATE OR REPLACE FUNCTION public.security_audit_admin_users_role()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    PERFORM public.write_security_audit_log(
        'admin_user_role_changed',
        NEW.company_id,
        'admin_users',
        NEW.id,
        'warning',
        jsonb_build_object(
            'previousRole', OLD.role,
            'newRole', NEW.role,
            'dbField', 'admin_users.role'
        )
    );
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_security_audit_admin_users_role ON public.admin_users;
CREATE TRIGGER trg_security_audit_admin_users_role
AFTER UPDATE OF role ON public.admin_users
FOR EACH ROW
WHEN (OLD.role IS DISTINCT FROM NEW.role)
EXECUTE FUNCTION public.security_audit_admin_users_role();

CREATE OR REPLACE FUNCTION public.security_audit_users_v2_status()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    PERFORM public.write_security_audit_log(
        'user_status_changed',
        NEW.company_id,
        'users_v2',
        NEW.id,
        'success',
        jsonb_build_object(
            'previousStatus', OLD.status,
            'newStatus', NEW.status,
            'dbField', 'users_v2.status'
        )
    );
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_security_audit_users_v2_status ON public.users_v2;
CREATE TRIGGER trg_security_audit_users_v2_status
AFTER UPDATE OF status ON public.users_v2
FOR EACH ROW
WHEN (OLD.status IS DISTINCT FROM NEW.status)
EXECUTE FUNCTION public.security_audit_users_v2_status();

CREATE OR REPLACE FUNCTION public.security_audit_companies_webhook_url()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_action text;
    v_url text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        v_action := 'company_webhook_url_created';
    ELSE
        v_action := 'company_webhook_url_updated';
    END IF;

    v_url := COALESCE(NEW.webhook_url, '');
    IF btrim(v_url) = '' THEN
        RETURN NEW;
    END IF;

    PERFORM public.write_security_audit_log(
        v_action,
        NEW.id,
        'companies',
        NEW.id,
        'success',
        jsonb_build_object(
            'webhookUrlPresent', true,
            'webhookUrlLength', length(v_url),
            'dbField', 'companies.webhook_url'
        )
    );
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_security_audit_companies_webhook_insert ON public.companies;
CREATE TRIGGER trg_security_audit_companies_webhook_insert
AFTER INSERT ON public.companies
FOR EACH ROW
WHEN (NEW.webhook_url IS NOT NULL AND btrim(NEW.webhook_url) <> '')
EXECUTE FUNCTION public.security_audit_companies_webhook_url();

DROP TRIGGER IF EXISTS trg_security_audit_companies_webhook_update ON public.companies;
CREATE TRIGGER trg_security_audit_companies_webhook_update
AFTER UPDATE OF webhook_url ON public.companies
FOR EACH ROW
WHEN (OLD.webhook_url IS DISTINCT FROM NEW.webhook_url)
EXECUTE FUNCTION public.security_audit_companies_webhook_url();

CREATE OR REPLACE FUNCTION public.security_audit_agent_http_tools_url()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_action text;
    v_url text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        v_action := 'http_tool_target_url_created';
    ELSE
        v_action := 'http_tool_target_url_updated';
    END IF;

    v_url := COALESCE(NEW.url, '');
    IF btrim(v_url) = '' THEN
        RETURN NEW;
    END IF;

    PERFORM public.write_security_audit_log(
        v_action,
        NEW.company_id,
        'agent_http_tools',
        NEW.id,
        'success',
        jsonb_build_object(
            'targetUrlPresent', true,
            'targetUrlLength', length(v_url),
            'dbField', 'agent_http_tools.target_url',
            'actualColumn', 'agent_http_tools.url'
        )
    );
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_security_audit_agent_http_tools_url_insert ON public.agent_http_tools;
CREATE TRIGGER trg_security_audit_agent_http_tools_url_insert
AFTER INSERT ON public.agent_http_tools
FOR EACH ROW
WHEN (NEW.url IS NOT NULL AND btrim(NEW.url) <> '')
EXECUTE FUNCTION public.security_audit_agent_http_tools_url();

DROP TRIGGER IF EXISTS trg_security_audit_agent_http_tools_url_update ON public.agent_http_tools;
CREATE TRIGGER trg_security_audit_agent_http_tools_url_update
AFTER UPDATE OF url ON public.agent_http_tools
FOR EACH ROW
WHEN (OLD.url IS DISTINCT FROM NEW.url)
EXECUTE FUNCTION public.security_audit_agent_http_tools_url();

CREATE OR REPLACE FUNCTION public.security_audit_resource_delete()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_company_id uuid;
BEGIN
    v_company_id := OLD.company_id;

    PERFORM public.write_security_audit_log(
        'resource_deleted',
        v_company_id,
        TG_TABLE_NAME,
        OLD.id,
        'success',
        jsonb_build_object('deletedResourceType', TG_TABLE_NAME)
    );
    RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS trg_security_audit_agents_delete ON public.agents;
CREATE TRIGGER trg_security_audit_agents_delete
AFTER DELETE ON public.agents
FOR EACH ROW
EXECUTE FUNCTION public.security_audit_resource_delete();

DROP TRIGGER IF EXISTS trg_security_audit_documents_delete ON public.documents;
CREATE TRIGGER trg_security_audit_documents_delete
AFTER DELETE ON public.documents
FOR EACH ROW
EXECUTE FUNCTION public.security_audit_resource_delete();

DROP TRIGGER IF EXISTS trg_security_audit_conversations_delete ON public.conversations;
CREATE TRIGGER trg_security_audit_conversations_delete
AFTER DELETE ON public.conversations
FOR EACH ROW
EXECUTE FUNCTION public.security_audit_resource_delete();

DROP TRIGGER IF EXISTS trg_security_audit_agent_http_tools_delete ON public.agent_http_tools;
CREATE TRIGGER trg_security_audit_agent_http_tools_delete
AFTER DELETE ON public.agent_http_tools
FOR EACH ROW
EXECUTE FUNCTION public.security_audit_resource_delete();

DROP TRIGGER IF EXISTS trg_security_audit_agent_mcp_connections_delete ON public.agent_mcp_connections;
CREATE TRIGGER trg_security_audit_agent_mcp_connections_delete
AFTER DELETE ON public.agent_mcp_connections
FOR EACH ROW
EXECUTE FUNCTION public.security_audit_resource_delete();

COMMIT;
