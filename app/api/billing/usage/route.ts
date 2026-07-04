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

export async function GET(request: NextRequest) {
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

    const url = new URL('/api/billing/usage-summary', BACKEND_URL);
    request.nextUrl.searchParams.forEach((value, key) => {
      url.searchParams.set(key, value);
    });

    const response = await fetch(url.toString(), {
      headers: {
        'Content-Type': 'application/json',
        'X-Admin-API-Key': adminApiKeyResult.adminApiKey,
        ...internalAuthHeaders,
      },
    });

    if (!response.ok) {
      log.warn('[Billing Usage API] Backend returned non-success', { status: response.status });
      return apiError('Erro ao carregar uso por agente', { request, status: response.status });
    }

    return NextResponse.json(await response.json());
  } catch (error: unknown) {
    log.error('[Billing Usage API] Error', errorLogFields(error));
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[Billing Usage API] Request failed',
      request,
      status: 500,
    });
  }
}
