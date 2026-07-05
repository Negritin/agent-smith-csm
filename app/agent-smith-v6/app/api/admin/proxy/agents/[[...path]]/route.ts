import { NextRequest, NextResponse } from 'next/server';
import { apiError, authApiError, upstreamApiError } from '@/lib/api-error';
import { auditCrossTenantAttempt, requireAdminSession } from '@/lib/auth-actions';
import { authenticatedProxy, getAdminApiKeyOrResponse } from '@/lib/admin-proxy';
import { createInternalAuthHeadersForAdminSession } from '@/lib/internal-jwt';
import { errorLogFields, log } from '@/lib/logger';
import { auditMasterAdminCompanyOverride } from '@/lib/security-audit';

export const dynamic = 'force-dynamic';

const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

function readCompanyId(payload: unknown): string | null {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
  const record = payload as Record<string, unknown>;
  const value = record.company_id || record.companyId;
  return typeof value === 'string' && value.length > 0 ? value : null;
}

async function createAgent(request: NextRequest) {
  try {
    const auth = await requireAdminSession();
    if (auth.response) return authApiError(auth.response, { request });

    const payload = await request.json();
    const companyId = readCompanyId(payload);
    if (!companyId) {
      return apiError('Contexto de empresa obrigatório', { request, status: 400 });
    }

    const { session } = auth;
    if (session.role === 'company_admin' && companyId !== session.companyId) {
      await auditCrossTenantAttempt({
        actorId: session.adminId,
        actorRole: session.role,
        actorCompanyId: session.companyId,
        resourceType: 'agents',
        resourceId: companyId,
        targetCompanyId: companyId,
        action: 'POST /api/agents',
        request,
      });

      return apiError('Recurso não encontrado', { request, status: 404 });
    }

    if (session.role === 'master_admin') {
      await auditMasterAdminCompanyOverride({
        request,
        actorId: session.adminId,
        sessionCompanyId: session.companyId || null,
        frontendCompanyId: companyId,
        resourceType: 'agents',
        resourceId: null,
        action: 'POST /api/agents',
        details: {
          backendPath: '/api/agents/',
        },
      });
    }

    const adminApiKey = getAdminApiKeyOrResponse(request);
    if (adminApiKey.response) return adminApiKey.response;

    const body =
      payload && typeof payload === 'object' && !Array.isArray(payload)
        ? { ...(payload as Record<string, unknown>), company_id: companyId }
        : payload;

    const backendResponse = await fetch(`${BACKEND_URL}/api/agents/`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Admin-API-Key': adminApiKey.adminApiKey,
        ...createInternalAuthHeadersForAdminSession(session, companyId),
      },
      body: JSON.stringify(body),
    });

    const responseBody = await backendResponse.text();
    if (!backendResponse.ok) {
      log.warn('[Admin Agents Create API] Backend returned non-success', {
        status: backendResponse.status,
      });
      return upstreamApiError(backendResponse.status, {
        fallback: 'Erro ao criar agente',
        request,
      });
    }

    return new NextResponse(responseBody, {
      status: backendResponse.status,
      headers: {
        'Content-Type': backendResponse.headers.get('Content-Type') || 'application/json',
      },
    });
  } catch (error: unknown) {
    log.error('[Admin Agents Create API] Error', errorLogFields(error));
    return apiError('Internal server error', {
      cause: error,
      logMessage: '[Admin Agents Create API] Request failed',
      request,
      status: 500,
    });
  }
}

async function handler(
  request: NextRequest,
  { params }: { params: Promise<{ path?: string[] }> },
) {
  const { path } = await params;
  if (request.method === 'POST' && (!path || path.length === 0)) {
    return createAgent(request);
  }

  const backendPath = path ? `/api/agents/${path.join('/')}` : '/api/agents';
  return authenticatedProxy(request, backendPath);
}

export const GET = handler;
export const POST = handler;
export const PUT = handler;
export const DELETE = handler;
export const PATCH = handler;
