import { NextRequest, NextResponse } from 'next/server';
import { getUserOrAdminSession } from '@/lib/auth-actions';
import { apiError } from '@/lib/api-error';
import { getAdminApiKeyOrResponse } from '@/lib/admin-proxy';
import {
  createInternalAuthHeadersForAdminSession,
  createInternalAuthHeadersForUserSession,
} from '@/lib/internal-jwt';
import { errorLogFields, log } from '@/lib/logger';

const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

export async function POST(request: NextRequest) {
  try {
    const auth = await getUserOrAdminSession();
    if (auth.response) {
      return apiError('Não autorizado', { request, status: auth.response.status || 401 });
    }

    const companyId = auth.userSession?.companyId || auth.adminSession?.companyId || null;
    if (!companyId) {
      return apiError('Contexto de empresa obrigatório', { request, status: 403 });
    }

    const adminApiKeyResult = getAdminApiKeyOrResponse(request);
    if (adminApiKeyResult.response) return adminApiKeyResult.response;

    const internalAuthHeaders = auth.userSession
      ? createInternalAuthHeadersForUserSession(auth.userSession, companyId)
      : createInternalAuthHeadersForAdminSession(auth.adminSession, companyId);

    const response = await fetch(`${BACKEND_URL}/api/billing/checkout/portal`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Admin-API-Key': adminApiKeyResult.adminApiKey,
        ...internalAuthHeaders,
      },
    });

    if (!response.ok) {
      log.warn('[Portal API] Backend returned non-success', { status: response.status });
      return apiError('Erro ao abrir portal de pagamento', { request, status: response.status });
    }

    return NextResponse.json(await response.json());
  } catch (error: unknown) {
    log.error('[Portal API] Error', errorLogFields(error));
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[Portal API] Request failed',
      request,
      status: 500,
    });
  }
}
