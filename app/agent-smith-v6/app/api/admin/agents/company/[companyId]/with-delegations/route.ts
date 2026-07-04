import { NextRequest, NextResponse } from 'next/server';
import { getAdminApiKeyOrResponse } from '@/lib/admin-proxy';
import { apiError } from '@/lib/api-error';
import { auditCrossTenantAttempt, requireAdminSession } from '@/lib/auth-actions';
import { createInternalAuthHeadersForAdminSession } from '@/lib/internal-jwt';
import { errorLogFields, log } from '@/lib/logger';
import { auditMasterAdminCompanyOverride } from '@/lib/security-audit';

const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ companyId: string }> },
) {
  try {
    const auth = await requireAdminSession();
    if (auth.response) {
      return apiError('Não autorizado', {
        request,
        status: auth.response.status || 401,
      });
    }

    const { session } = auth;
    const { companyId } = await params;

    if (session.role === 'company_admin' && companyId !== session.companyId) {
      await auditCrossTenantAttempt({
        actorId: session.adminId,
        actorRole: session.role,
        actorCompanyId: session.companyId,
        resourceType: 'companies',
        resourceId: companyId,
        targetCompanyId: companyId,
        action: 'proxy_company_agents_with_delegations',
        request,
      });

      return apiError('Empresa não encontrada', { request, status: 404 });
    }

    if (session.role === 'master_admin') {
      await auditMasterAdminCompanyOverride({
        request,
        actorId: session.adminId,
        sessionCompanyId: session.companyId || null,
        frontendCompanyId: companyId,
        resourceType: 'companies',
        resourceId: companyId,
        action: 'proxy_company_agents_with_delegations',
      });
    }

    const adminApiKey = getAdminApiKeyOrResponse(request);
    if (adminApiKey.response) return adminApiKey.response;

    const response = await fetch(
      `${BACKEND_URL}/api/agents/admin/company/${companyId}/with-delegations`,
      {
        headers: {
          'Content-Type': 'application/json',
          'X-Admin-API-Key': adminApiKey.adminApiKey,
          ...createInternalAuthHeadersForAdminSession(session, companyId),
        },
      },
    );

    if (!response.ok) {
      log.warn('[Admin Agents+Delegations API] Backend returned non-success', {
        status: response.status,
      });
      return apiError('Failed to load agents', {
        request,
        status: response.status,
      });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error: unknown) {
    log.error('[Admin Agents+Delegations API] Error', errorLogFields(error));
    return apiError('Internal server error', {
      cause: error,
      logMessage: '[Admin Agents+Delegations API] Request failed',
      request,
      status: 500,
    });
  }
}
