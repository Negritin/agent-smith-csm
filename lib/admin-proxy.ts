import { NextRequest, NextResponse } from 'next/server';
import { requireAdminSession } from './auth-actions';
import { apiError, authApiError, upstreamApiError } from './api-error';
import type { AdminSessionData } from './iron-session';
import { createInternalAuthHeadersForAdminSession } from './internal-jwt';
import { errorLogFields, log } from './logger';
import {
  auditMasterAdminCompanyOverride,
  logSecurityAudit,
  summarizeAuditUrl,
} from './security-audit';
import { getSupabaseAdmin } from './supabase-admin';

const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
const UUID_PATTERN = '[0-9a-fA-F-]{36}';

type AuditResource = {
  resourceType: string;
  resourceId: string | null;
  details?: Record<string, unknown>;
};

function parseJsonObject(value: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(value);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
    return parsed as Record<string, unknown>;
  } catch {
    return null;
  }
}

function inferAuditResourceFromPath(backendPath: string): AuditResource | null {
  const httpToolMatch = backendPath.match(new RegExp(`^/api/agents/tools/(${UUID_PATTERN})(?:/|$)`));
  if (httpToolMatch?.[1]) {
    return { resourceType: 'agent_http_tools', resourceId: httpToolMatch[1] };
  }

  const agentMatch = backendPath.match(new RegExp(`^/api/agents/(${UUID_PATTERN})(?:/|$)`));
  if (agentMatch?.[1]) return { resourceType: 'agents', resourceId: agentMatch[1] };

  const documentMatch = backendPath.match(new RegExp(`^/documents/(${UUID_PATTERN})(?:/|$)`));
  if (documentMatch?.[1]) return { resourceType: 'documents', resourceId: documentMatch[1] };

  const mcpConnectionMatch = backendPath.match(new RegExp(`^/api/mcp/connections/([^/]+)(?:/|$)`));
  if (mcpConnectionMatch?.[1]) {
    return { resourceType: 'agent_mcp_connections', resourceId: mcpConnectionMatch[1] };
  }

  const mcpDisableMatch = backendPath.match(
    new RegExp(`^/api/mcp/agent/(${UUID_PATTERN})/disable-server/([^/]+)(?:/|$)`),
  );
  if (mcpDisableMatch?.[2]) {
    return {
      resourceType: 'agent_mcp_connections',
      resourceId: mcpDisableMatch[2],
      details: {
        agentId: mcpDisableMatch[1],
        mcpServerId: mcpDisableMatch[2],
      },
    };
  }

  return null;
}

function inferResponseResourceId(responseBody: string): string | null {
  const responseJson = parseJsonObject(responseBody);
  if (!responseJson) return null;

  const id =
    responseJson.id ||
    responseJson.tool_id ||
    responseJson.document_id ||
    responseJson.agent_id ||
    null;

  return typeof id === 'string' ? id : null;
}

function inferHttpToolUrlAudit(params: {
  backendPath: string;
  method: string;
  bodyJson: Record<string, unknown> | null;
  responseBody: string;
}): AuditResource | null {
  if (!params.bodyJson || typeof params.bodyJson.url !== 'string') return null;

  if (params.method === 'POST' && params.backendPath === '/api/agents/tools') {
    return {
      resourceType: 'agent_http_tools',
      resourceId: inferResponseResourceId(params.responseBody),
      details: {
        targetUrl: summarizeAuditUrl(params.bodyJson.url),
      },
    };
  }

  if (params.method === 'PUT') {
    const toolMatch = params.backendPath.match(
      new RegExp(`^/api/agents/tools/(${UUID_PATTERN})(?:/|$)`),
    );
    if (toolMatch?.[1]) {
      return {
        resourceType: 'agent_http_tools',
        resourceId: toolMatch[1],
        details: {
          targetUrl: summarizeAuditUrl(params.bodyJson.url),
        },
      };
    }
  }

  return null;
}

async function auditSuccessfulProxySecurityEvent(params: {
  request: NextRequest;
  session: AdminSessionData;
  backendPath: string;
  targetCompanyId: string | null;
  bodyJson: Record<string, unknown> | null;
  responseBody: string;
}): Promise<void> {
  const companyId = params.targetCompanyId || params.session.companyId || null;
  const httpToolAudit = inferHttpToolUrlAudit({
    backendPath: params.backendPath,
    method: params.request.method,
    bodyJson: params.bodyJson,
    responseBody: params.responseBody,
  });

  if (httpToolAudit) {
    await logSecurityAudit({
      action:
        params.request.method === 'POST'
          ? 'http_tool_target_url_created'
          : 'http_tool_target_url_updated',
      actorId: params.session.adminId,
      actorRole: params.session.role,
      companyId,
      targetCompanyId: companyId,
      resourceType: httpToolAudit.resourceType,
      resourceId: httpToolAudit.resourceId,
      request: params.request,
      status: 'success',
      details: {
        ...(httpToolAudit.details || {}),
        dbField: 'agent_http_tools.target_url',
        actualColumn: 'agent_http_tools.url',
        backendPath: params.backendPath,
      },
    });
  }

  if (params.request.method !== 'DELETE') return;

  const deleteAudit = inferAuditResourceFromPath(params.backendPath);
  if (!deleteAudit) return;

  await logSecurityAudit({
    action: 'resource_deleted',
    actorId: params.session.adminId,
    actorRole: params.session.role,
    companyId,
    targetCompanyId: companyId,
    resourceType: deleteAudit.resourceType,
    resourceId: deleteAudit.resourceId,
    request: params.request,
    status: 'success',
    details: {
      ...(deleteAudit.details || {}),
      deletedResourceType: deleteAudit.resourceType,
      backendPath: params.backendPath,
    },
  });
}

export function getAdminApiKeyOrResponse(request?: Request):
  | { adminApiKey: string; response?: never }
  | { adminApiKey?: never; response: NextResponse } {
  const adminApiKey = process.env.ADMIN_API_KEY;

  if (!adminApiKey) {
    log.error('[ADMIN PROXY] ADMIN_API_KEY is not configured');
    return {
      response: apiError('Erro interno ao conectar com backend', {
        logMessage: '[ADMIN PROXY] Missing backend API key',
        request,
        status: 500,
      }),
    };
  }

  return { adminApiKey };
}

function inferTargetCompanyId(request: NextRequest, backendPath: string): string | null {
  const fromQuery =
    request.nextUrl.searchParams.get('company_id') ||
    request.nextUrl.searchParams.get('companyId');
  if (fromQuery) return fromQuery;

  const companyPathMatch = backendPath.match(
    new RegExp(`/(?:company|chunks)/(${UUID_PATTERN})(?:/|$)`),
  );
  if (companyPathMatch?.[1]) return companyPathMatch[1];

  const agentConfigPathMatch = backendPath.match(
    new RegExp(`^/api/agent/(?:config|test)/(${UUID_PATTERN})(?:/|$)`),
  );
  if (agentConfigPathMatch?.[1]) return agentConfigPathMatch[1];

  return null;
}

function readStringField(value: unknown, keys: string[]): string | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;

  const record = value as Record<string, unknown>;
  for (const key of keys) {
    const field = record[key];
    if (typeof field === 'string' && field.length > 0) return field;
  }

  return null;
}

/**
 * Helper genérico de resolução de tenant (MEDIO-008).
 *
 * Centraliza o molde repetido das funções resolve*CompanyId: select +
 * maybeSingle + try/catch/log. Cada recurso difere apenas na tabela, na coluna
 * de id, nas colunas selecionadas, na mensagem de log e em como o company_id é
 * derivado da linha (direto ou via FK encadeada). O comportamento observável
 * (incluindo a mensagem de cada log.warn) é idêntico ao das funções originais.
 */
async function resolveCompanyIdFromTable(
  config: {
    table: string;
    select: string;
    logLabel: string;
    idColumn?: string;
    map: (row: Record<string, unknown>) => string | null | Promise<string | null>;
  },
  idValue: string,
): Promise<string | null> {
  try {
    const { data } = await getSupabaseAdmin()
      .from(config.table)
      .select(config.select)
      .eq(config.idColumn ?? 'id', idValue)
      .maybeSingle();

    if (!data) return null;
    return await config.map(data as unknown as Record<string, unknown>);
  } catch (error: unknown) {
    log.warn(config.logLabel, errorLogFields(error));
    return null;
  }
}

function resolveAgentCompanyId(agentId: string): Promise<string | null> {
  return resolveCompanyIdFromTable(
    {
      table: 'agents',
      select: 'company_id',
      logLabel: '[ADMIN PROXY] Could not resolve agent company',
      map: (row) => (typeof row.company_id === 'string' ? row.company_id : null),
    },
    agentId,
  );
}

function resolveToolCompanyId(toolId: string): Promise<string | null> {
  return resolveCompanyIdFromTable(
    {
      table: 'agent_http_tools',
      select: 'agent_id',
      logLabel: '[ADMIN PROXY] Could not resolve tool company',
      map: (row) => (typeof row.agent_id === 'string' ? resolveAgentCompanyId(row.agent_id) : null),
    },
    toolId,
  );
}

function resolveDocumentCompanyId(documentId: string): Promise<string | null> {
  return resolveCompanyIdFromTable(
    {
      table: 'documents',
      select: 'company_id',
      logLabel: '[ADMIN PROXY] Could not resolve document company',
      map: (row) => (typeof row.company_id === 'string' ? row.company_id : null),
    },
    documentId,
  );
}

function resolveMcpConnectionCompanyId(connectionId: string): Promise<string | null> {
  return resolveCompanyIdFromTable(
    {
      table: 'agent_mcp_connections',
      select: 'agent_id',
      logLabel: '[ADMIN PROXY] Could not resolve MCP connection company',
      map: (row) => (typeof row.agent_id === 'string' ? resolveAgentCompanyId(row.agent_id) : null),
    },
    connectionId,
  );
}

function resolveUcpConnectionCompanyId(connectionId: string): Promise<string | null> {
  return resolveCompanyIdFromTable(
    {
      table: 'ucp_connections',
      select: 'company_id, agent_id',
      logLabel: '[ADMIN PROXY] Could not resolve UCP connection company',
      map: (row) => {
        if (typeof row.company_id === 'string') return row.company_id;
        if (typeof row.agent_id === 'string') return resolveAgentCompanyId(row.agent_id);
        return null;
      },
    },
    connectionId,
  );
}

function resolveDelegationCompanyId(delegationId: string): Promise<string | null> {
  return resolveCompanyIdFromTable(
    {
      table: 'agent_delegations',
      select: 'orchestrator_id',
      logLabel: '[ADMIN PROXY] Could not resolve delegation company',
      map: (row) =>
        typeof row.orchestrator_id === 'string' ? resolveAgentCompanyId(row.orchestrator_id) : null,
    },
    delegationId,
  );
}

async function inferBodyTargetCompanyId(value: unknown, backendPath: string): Promise<string | null> {
  const companyId = readStringField(value, ['company_id', 'companyId']);
  if (companyId) return companyId;

  const agentId = readStringField(value, [
    'agent_id',
    'agentId',
    'orchestrator_id',
    'orchestratorId',
    'subagent_id',
    'subagentId',
  ]);
  if (agentId) return resolveAgentCompanyId(agentId);

  const documentId = readStringField(value, ['document_id', 'documentId']);
  if (documentId) return resolveDocumentCompanyId(documentId);

  const toolId = readStringField(value, ['id', 'tool_id', 'toolId']);
  if (toolId && backendPath.includes('/tools')) return resolveToolCompanyId(toolId);

  return null;
}

async function inferPathResourceCompanyId(backendPath: string): Promise<string | null> {
  // UCP precede o match genérico de /tools/ abaixo (que assume agent_http_tools).
  const ucpAgentPathMatch = backendPath.match(
    new RegExp(`^/api/ucp/(?:connections|tools)/(${UUID_PATTERN})(?:/|$)`),
  );
  if (ucpAgentPathMatch?.[1]) return resolveAgentCompanyId(ucpAgentPathMatch[1]);

  const ucpConnectionPathMatch = backendPath.match(
    new RegExp(`^/api/ucp/(?:disconnect|refresh)/(${UUID_PATTERN})(?:/|$)`),
  );
  if (ucpConnectionPathMatch?.[1]) {
    return resolveUcpConnectionCompanyId(ucpConnectionPathMatch[1]);
  }

  const delegationPathMatch = backendPath.match(
    new RegExp(`^/api/agents/delegations/(${UUID_PATTERN})(?:/|$)`),
  );
  if (delegationPathMatch?.[1]) return resolveDelegationCompanyId(delegationPathMatch[1]);

  const toolPathMatch = backendPath.match(new RegExp(`/tools/(${UUID_PATTERN})(?:/|$)`));
  if (toolPathMatch?.[1]) return resolveToolCompanyId(toolPathMatch[1]);

  const agentPathMatch =
    backendPath.match(new RegExp(`^/api/agents/(${UUID_PATTERN})(?:/|$)`)) ||
    backendPath.match(new RegExp(`^/api/mcp/agent/(${UUID_PATTERN})(?:/|$)`));
  if (agentPathMatch?.[1]) return resolveAgentCompanyId(agentPathMatch[1]);

  const documentPathMatch = backendPath.match(new RegExp(`^/documents/(${UUID_PATTERN})(?:/|$)`));
  if (documentPathMatch?.[1]) return resolveDocumentCompanyId(documentPathMatch[1]);

  const mcpConnectionPathMatch = backendPath.match(
    new RegExp(`^/api/mcp/connections/([^/]+)(?:/|$)`),
  );
  if (mcpConnectionPathMatch?.[1]) {
    return resolveMcpConnectionCompanyId(mcpConnectionPathMatch[1]);
  }

  return null;
}

async function inferQueryResourceCompanyId(request: NextRequest): Promise<string | null> {
  const agentId = request.nextUrl.searchParams.get('agent_id') ||
    request.nextUrl.searchParams.get('agentId');
  if (agentId) return resolveAgentCompanyId(agentId);

  const documentId = request.nextUrl.searchParams.get('document_id') ||
    request.nextUrl.searchParams.get('documentId');
  if (documentId) return resolveDocumentCompanyId(documentId);

  return null;
}

function isMultiTenantBackendPath(backendPath: string, request: NextRequest): boolean {
  if (backendPath === '/api/agents' || backendPath === '/api/agents/') return true;
  if (backendPath.startsWith('/api/agents/company/')) return true;
  if (backendPath.startsWith('/api/agents/admin/company/')) return true;
  if (backendPath.startsWith('/api/agents/tools')) return true;
  if (new RegExp(`^/api/agents/${UUID_PATTERN}(?:/|$)`).test(backendPath)) return true;
  if (backendPath.startsWith('/api/agents/delegations')) return true;

  if (backendPath.startsWith('/api/agent/config/')) return true;
  if (backendPath.startsWith('/api/agent/test/')) return true;

  if (backendPath === '/documents' || backendPath === '/documents/') return true;
  if (backendPath.startsWith('/documents/upload')) return true;
  if (backendPath.startsWith('/documents/chunks/')) return true;
  if (backendPath.startsWith('/documents/agent/')) return true;
  if (backendPath.startsWith('/documents/benchmark/eligible')) return true;
  if (backendPath.startsWith('/documents/benchmark/start')) return true;
  if (backendPath.startsWith('/documents/benchmark/status/')) return true;
  if (backendPath.startsWith('/documents/reprocess')) return true;
  if (new RegExp(`^/documents/${UUID_PATTERN}(?:/|$)`).test(backendPath)) return true;

  if (new RegExp(`^/api/mcp/agent/${UUID_PATTERN}(?:/|$)`).test(backendPath)) return true;
  if (backendPath.startsWith('/api/mcp/connections/')) return true;
  if (backendPath.match(/^\/api\/mcp\/servers\/[^/]+\/tools$/)) return true;

  // UCP (Universal Commerce Protocol) — todas as rotas exigem tenant JWT.
  if (backendPath.startsWith('/api/ucp/connections/')) return true;
  if (backendPath.startsWith('/api/ucp/tools/')) return true;
  if (backendPath.startsWith('/api/ucp/disconnect/')) return true;
  if (backendPath.startsWith('/api/ucp/refresh/')) return true;
  if (backendPath === '/api/ucp/connect') return true;
  if (backendPath === '/api/ucp/discover') return true;
  if (backendPath === '/api/ucp/execute') return true;
  if (backendPath.startsWith('/api/mcp/oauth/url/')) {
    return Boolean(
      request.nextUrl.searchParams.get('agent_id') ||
      request.nextUrl.searchParams.get('agentId') ||
      request.nextUrl.searchParams.get('company_id') ||
      request.nextUrl.searchParams.get('companyId'),
    );
  }

  return false;
}

/**
 * Proxy autenticado: encaminha request ao backend com X-Admin-API-Key.
 * Segue o mesmo padrão de app/api/admin/agents/company/[companyId]/route.ts
 */
export async function authenticatedProxy(
  request: NextRequest,
  backendPath: string,
): Promise<NextResponse> {
  try {
    const auth = await requireAdminSession();
    if (auth.response) {
      return authApiError(auth.response, { request });
    }

    if (auth.session.role === 'company_admin' && !auth.session.companyId) {
      return apiError('Não autorizado', { request, status: 403 });
    }

    const adminApiKeyResult = getAdminApiKeyOrResponse(request);
    if (adminApiKeyResult.response) return adminApiKeyResult.response;

    // 1. Construir URL do backend preservando query params
    const url = new URL(backendPath, BACKEND_URL);
    request.nextUrl.searchParams.forEach((value, key) => {
      url.searchParams.set(key, value);
    });

    // 2. Preparar body
    const contentType = request.headers.get('content-type');
    let body: BodyInit | undefined;
    let bodyTargetCompanyId: string | null = null;
    let bodyJson: Record<string, unknown> | null = null;
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      if (contentType?.includes('multipart/form-data')) {
        const formData = await request.formData();
        body = formData;
        bodyTargetCompanyId =
          (formData.get('company_id') || formData.get('companyId'))?.toString() || null;
        if (!bodyTargetCompanyId) {
          const agentId = (formData.get('agent_id') || formData.get('agentId'))?.toString();
          bodyTargetCompanyId = agentId ? await resolveAgentCompanyId(agentId) : null;
        }
      } else {
        const rawBody = await request.text();
        body = rawBody;
        if (contentType?.includes('application/json')) {
          try {
            bodyJson = parseJsonObject(rawBody);
            bodyTargetCompanyId = bodyJson
              ? await inferBodyTargetCompanyId(bodyJson, backendPath)
              : null;
          } catch {
            bodyTargetCompanyId = null;
          }
        }
      }
    }

    // 3. Preparar headers (mesmo padrão das rotas existentes)
    let targetCompanyId =
      inferTargetCompanyId(request, backendPath) ||
      bodyTargetCompanyId ||
      (await inferPathResourceCompanyId(backendPath)) ||
      (await inferQueryResourceCompanyId(request)) ||
      null;
    const requiresTenantJwt = isMultiTenantBackendPath(backendPath, request);

    if (auth.session.role === 'company_admin') {
      if (targetCompanyId && targetCompanyId !== auth.session.companyId) {
        const auditResource = inferAuditResourceFromPath(backendPath);
        await logSecurityAudit({
          action: 'cross_tenant_attempt',
          actorId: auth.session.adminId,
          actorRole: auth.session.role,
          companyId: auth.session.companyId,
          targetCompanyId,
          resourceType: auditResource?.resourceType || 'companies',
          resourceId: auditResource?.resourceId || targetCompanyId,
          request,
          status: 'error',
          details: {
            ...(auditResource?.details || {}),
            attemptedAction: `${request.method} ${backendPath}`,
            backendPath,
          },
        });
        return apiError('Recurso não encontrado', { request, status: 404 });
      }
      targetCompanyId = auth.session.companyId || null;
    }

    if (auth.session.role === 'master_admin' && targetCompanyId) {
      const auditResource = inferAuditResourceFromPath(backendPath);
      await auditMasterAdminCompanyOverride({
        request,
        actorId: auth.session.adminId,
        sessionCompanyId: auth.session.companyId || null,
        frontendCompanyId: targetCompanyId,
        resourceType: auditResource?.resourceType || 'companies',
        resourceId: auditResource?.resourceId || targetCompanyId,
        action: `${request.method} ${backendPath}`,
        details: {
          ...(auditResource?.details || {}),
          backendPath,
        },
      });
    }

    if (!targetCompanyId && requiresTenantJwt) {
      log.warn('[ADMIN PROXY] Missing target company for multi-tenant request', {
        backendPath,
        adminRole: auth.session.role,
      });
      return apiError('Contexto de empresa obrigatório', { request, status: 400 });
    }

    const internalAuthHeaders = targetCompanyId
      ? createInternalAuthHeadersForAdminSession(auth.session, targetCompanyId)
      : {};

    const headers: Record<string, string> = {
      'X-Admin-API-Key': adminApiKeyResult.adminApiKey,
      ...internalAuthHeaders,
    };

    if (contentType && !contentType.includes('multipart/form-data')) {
      headers['Content-Type'] = contentType;
    }

    // 4. Forward ao backend
    const backendResponse = await fetch(url.toString(), {
      method: request.method,
      headers,
      body,
    });

    // 5. Retornar response
    const responseBody = await backendResponse.text();
    if (!backendResponse.ok) {
      log.warn('[ADMIN PROXY] Backend returned non-success', {
        backendPath,
        status: backendResponse.status,
      });
      return upstreamApiError(backendResponse.status, {
        fallback: 'Erro ao processar solicitação administrativa',
        request,
      });
    }

    await auditSuccessfulProxySecurityEvent({
      request,
      session: auth.session,
      backendPath,
      targetCompanyId,
      bodyJson,
      responseBody,
    });

    return new NextResponse(responseBody, {
      status: backendResponse.status,
      headers: {
        'Content-Type': backendResponse.headers.get('Content-Type') || 'application/json',
      },
    });
  } catch (error: unknown) {
    log.error('[ADMIN PROXY] Backend error', errorLogFields(error));
    return apiError('Erro ao conectar com backend', {
      cause: error,
      logMessage: '[ADMIN PROXY] Backend request failed',
      request,
      status: 502,
    });
  }
}
