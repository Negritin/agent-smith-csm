import { NextRequest, NextResponse } from 'next/server';
import { getUserOrAdminSession } from '@/lib/auth-actions';
import { apiError, authApiError } from '@/lib/api-error';
import {
  createInternalAuthHeadersForAdminSession,
  createInternalAuthHeadersForUserSession,
} from '@/lib/internal-jwt';
import { errorLogFields, log } from '@/lib/logger';
import { getAdminApiKeyOrResponse } from '@/lib/admin-proxy';

const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

const EMPTY_SUBSCRIPTION = {
  has_subscription: false,
  status: null,
  plan: null,
  balance_brl: 0,
  credits_display: { remaining: 0, used: 0, total: 0, percentage: 0 },
  usage: { agents: { used: 0, limit: 0 }, knowledge_bases: { used: 0, limit: 0 } },
  current_period_end: null,
};

export async function GET(request: NextRequest) {
  try {
    const auth = await getUserOrAdminSession();
    if (auth.response) {
      return authApiError(auth.response, { request });
    }

    const companyId = auth.userSession?.companyId || auth.adminSession?.companyId || null;
    if (!companyId) {
      return NextResponse.json(EMPTY_SUBSCRIPTION);
    }

    const internalAuthHeaders = auth.userSession
      ? createInternalAuthHeadersForUserSession(auth.userSession, companyId)
      : createInternalAuthHeadersForAdminSession(auth.adminSession, companyId);
    const adminApiKeyResult = getAdminApiKeyOrResponse(request);
    if (adminApiKeyResult.response) return adminApiKeyResult.response;

    const response = await fetch(`${BACKEND_URL}/api/billing/my-subscription`, {
      headers: {
        'Content-Type': 'application/json',
        'X-Admin-API-Key': adminApiKeyResult.adminApiKey,
        ...internalAuthHeaders,
      },
    });

    if (!response.ok) {
      log.warn('[Billing API] Backend returned non-success', { status: response.status });
      return apiError('Erro ao carregar assinatura', {
        request,
        status: response.status,
      });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error: unknown) {
    log.error('[Billing API] Error', errorLogFields(error));
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[Billing API] Subscription proxy failed',
      request,
      status: 500,
    });
  }
}
