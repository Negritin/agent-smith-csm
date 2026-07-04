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

class InvalidCheckoutBodyError extends Error {}

function parseCheckoutBody(body: unknown): { plan_id: string } {
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    throw new InvalidCheckoutBodyError();
  }

  const allowedKeys = new Set(['plan_id']);
  const keys = Object.keys(body);
  if (keys.some((key) => !allowedKeys.has(key))) {
    throw new InvalidCheckoutBodyError();
  }

  const planId = (body as Record<string, unknown>).plan_id;
  if (typeof planId !== 'string' || !planId.trim()) {
    throw new InvalidCheckoutBodyError();
  }

  return { plan_id: planId };
}

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

    const body = parseCheckoutBody(await request.json().catch(() => null));
    const adminApiKeyResult = getAdminApiKeyOrResponse(request);
    if (adminApiKeyResult.response) return adminApiKeyResult.response;

    const internalAuthHeaders = auth.userSession
      ? createInternalAuthHeadersForUserSession(auth.userSession, companyId)
      : createInternalAuthHeadersForAdminSession(auth.adminSession, companyId);

    const response = await fetch(`${BACKEND_URL}/api/billing/checkout/subscription`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Admin-API-Key': adminApiKeyResult.adminApiKey,
        ...internalAuthHeaders,
      },
      body: JSON.stringify(body),
    });

    if (!response.ok) {
      log.warn('[Checkout API] Backend returned non-success', { status: response.status });
      return apiError('Erro ao criar checkout', { request, status: response.status });
    }

    return NextResponse.json(await response.json());
  } catch (error: unknown) {
    log.error('[Checkout API] Error', errorLogFields(error));

    if (error instanceof InvalidCheckoutBodyError) {
      return apiError('Parâmetro permitido: plan_id', { request, status: 400 });
    }

    return apiError('Erro interno', {
      cause: error,
      logMessage: '[Checkout API] Request failed',
      request,
      status: 500,
    });
  }
}
