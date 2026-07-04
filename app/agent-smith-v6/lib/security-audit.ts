import { getClientInfo } from './logger';
import { getSupabaseAdmin } from './supabase-admin';

export type SecurityAuditStatus = 'success' | 'error' | 'warning';
export type SecurityAuditActorRole = 'master_admin' | 'company_admin' | 'user' | string;

type SecurityAuditParams = {
  action: string;
  actorId?: string | null;
  actorRole?: SecurityAuditActorRole | null;
  companyId?: string | null;
  targetCompanyId?: string | null;
  resourceType?: string | null;
  resourceId?: string | null;
  request?: Request;
  status?: SecurityAuditStatus;
  details?: Record<string, unknown>;
  errorMessage?: string | null;
  correlationId?: string | null;
};

type MasterAdminTenantAuditParams = {
  request?: Request;
  actorId: string;
  sessionCompanyId?: string | null;
  frontendCompanyId?: string | null;
  resourceType: string;
  resourceId?: string | null;
  action: string;
  details?: Record<string, unknown>;
};

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function uuidOrNull(value?: string | null): string | null {
  if (!value || !UUID_RE.test(value)) return null;
  return value;
}

function readCorrelationId(request?: Request, fallback?: string | null): string | null {
  return (
    request?.headers.get('x-correlation-id') ||
    request?.headers.get('x-request-id') ||
    fallback ||
    null
  );
}

function actorColumns(actorId?: string | null, actorRole?: SecurityAuditActorRole | null) {
  const safeActorId = uuidOrNull(actorId);
  if (!safeActorId) return { admin_id: null, user_id: null };

  return actorRole === 'master_admin'
    ? { admin_id: safeActorId, user_id: null }
    : { admin_id: null, user_id: safeActorId };
}

export function summarizeAuditUrl(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  if (!trimmed) return { present: false };

  try {
    const url = new URL(trimmed);
    return {
      present: true,
      protocol: url.protocol,
      host: url.host,
      path: url.pathname.slice(0, 256),
      queryKeys: Array.from(url.searchParams.keys()).slice(0, 20),
      length: trimmed.length,
    };
  } catch {
    return {
      present: true,
      parseable: false,
      length: trimmed.length,
    };
  }
}

export async function logSecurityAudit(params: SecurityAuditParams): Promise<void> {
  try {
    const supabaseAdmin = getSupabaseAdmin();
    const { ipAddress = undefined, userAgent = undefined } = params.request
      ? getClientInfo(params.request)
      : {};
    const correlationId = readCorrelationId(params.request, params.correlationId);
    const targetId = params.resourceId || null;
    const targetCompanyId = params.targetCompanyId || null;
    const actor = actorColumns(params.actorId, params.actorRole);

    const { error } = await supabaseAdmin.from('system_logs').insert({
      timestamp: new Date().toISOString(),
      ...actor,
      company_id: uuidOrNull(params.companyId),
      action_type: params.action,
      resource_type: params.resourceType || null,
      resource_id: uuidOrNull(params.resourceId),
      status: params.status || 'success',
      error_message: params.errorMessage || null,
      ip_address: ipAddress || null,
      user_agent: userAgent || null,
      details: {
        ...(params.details || {}),
        category: 'security_audit',
        action: params.action,
        actorRole: params.actorRole || null,
        actorId: params.actorId || null,
        targetId,
        targetCompanyId,
        correlationId,
      },
    });

    if (error) {
      console.error('[SECURITY AUDIT] Failed to write audit log:', error);
    }
  } catch (error) {
    console.error('[SECURITY AUDIT] Failed to write audit log:', error);
  }
}

export async function auditMasterAdminCompanyOverride(
  params: MasterAdminTenantAuditParams,
): Promise<void> {
  const frontendCompanyId = params.frontendCompanyId || null;
  const sessionCompanyId = params.sessionCompanyId || null;

  if (!frontendCompanyId || frontendCompanyId === sessionCompanyId) return;

  await logSecurityAudit({
    action: 'master_admin_cross_tenant_access',
    actorId: params.actorId,
    actorRole: 'master_admin',
    companyId: frontendCompanyId,
    targetCompanyId: frontendCompanyId,
    resourceType: params.resourceType,
    resourceId: params.resourceId || frontendCompanyId,
    request: params.request,
    status: 'warning',
    details: {
      ...params.details,
      attemptedAction: params.action,
      frontendCompanyId,
      sessionCompanyId,
    },
  });
}
