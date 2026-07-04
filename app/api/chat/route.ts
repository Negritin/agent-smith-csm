import { NextRequest, NextResponse } from 'next/server';
import { getUserOrAdminSession } from '@/lib/auth-actions';
import { apiError } from '@/lib/api-error';
import { getAdminApiKeyOrResponse } from '@/lib/admin-proxy';
import {
  createInternalAuthHeadersForAdminSession,
  createInternalAuthHeadersForUserSession,
} from '@/lib/internal-jwt';
import { errorLogFields, log } from '@/lib/logger';
import { auditMasterAdminCompanyOverride, logSecurityAudit } from '@/lib/security-audit';

export const dynamic = 'force-dynamic';

function isWidgetChatBody(body: unknown): boolean {
  if (!body || typeof body !== 'object' || Array.isArray(body)) return false;
  const value = body as Record<string, unknown>;
  return value.channel === 'widget' || !value.userId;
}

async function getTrustedChatHeaders(
  request: NextRequest,
  body: Record<string, unknown>,
): Promise<HeadersInit | NextResponse> {
  const adminApiKeyResult = getAdminApiKeyOrResponse(request);
  if (adminApiKeyResult.response) return adminApiKeyResult.response;

  const baseHeaders: Record<string, string> = {
    'Content-Type': 'application/json',
    'X-Admin-API-Key': adminApiKeyResult.adminApiKey,
  };

  if (isWidgetChatBody(body)) {
    const widgetToken = request.headers.get('x-widget-token');
    if (!widgetToken) {
      return apiError('Widget token obrigatório', { request, status: 401 });
    }
    return {
      ...baseHeaders,
      'X-Widget-Token': widgetToken,
    };
  }

  const auth = await getUserOrAdminSession();
  if (auth.response) {
    return apiError('Não autorizado', { request, status: auth.response.status || 401 });
  }

  const requestedCompanyId = typeof body.companyId === 'string' ? body.companyId : null;
  let targetCompanyId = auth.userSession?.companyId || auth.adminSession?.companyId || null;

  if (auth.adminSession?.role === 'master_admin' && requestedCompanyId) {
    targetCompanyId = requestedCompanyId;
    await auditMasterAdminCompanyOverride({
      request,
      actorId: auth.adminSession.adminId,
      sessionCompanyId: auth.adminSession.companyId || null,
      frontendCompanyId: requestedCompanyId,
      resourceType: 'chat',
      resourceId: requestedCompanyId,
      action: 'post_chat',
    });
  }

  if (!targetCompanyId) {
    return apiError('Contexto de empresa obrigatório', { request, status: 403 });
  }

  if (requestedCompanyId && requestedCompanyId !== targetCompanyId) {
    await logSecurityAudit({
      action: 'cross_tenant_attempt',
      actorId: auth.userSession?.userId || auth.adminSession?.adminId || null,
      actorRole: auth.userSession ? 'user' : auth.adminSession?.role || null,
      companyId: targetCompanyId,
      targetCompanyId: requestedCompanyId,
      resourceType: 'chat',
      resourceId: requestedCompanyId,
      request,
      status: 'error',
      details: {
        attemptedAction: 'post_chat',
      },
    });

    return apiError('Recurso não encontrado', { request, status: 404 });
  }

  const internalAuthHeaders = auth.userSession
    ? createInternalAuthHeadersForUserSession(auth.userSession, targetCompanyId)
    : createInternalAuthHeadersForAdminSession(auth.adminSession, targetCompanyId);

  return {
    ...baseHeaders,
    ...internalAuthHeaders,
  };
}

/**
 * POST /api/chat
 *
 * Proxy simples para o backend Python (/chat).
 * Usado pelo Widget embeddable que espera resposta JSON completa.
 */
export async function POST(req: NextRequest) {
  try {
    const body = await req.json();
    if (!body || typeof body !== 'object' || Array.isArray(body)) {
      return apiError('Requisição inválida', { request: req, status: 400 });
    }

    const trustedHeaders = await getTrustedChatHeaders(req, body as Record<string, unknown>);
    if (trustedHeaders instanceof NextResponse) return trustedHeaders;

    const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL || 'http://localhost:8000';

    // Timeout de 90s — LLMs podem demorar, mas não devem travar infinitamente
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 90_000);

    let response: Response;
    try {
      response = await fetch(`${backendUrl}/chat`, {
        method: 'POST',
        headers: trustedHeaders,
        body: JSON.stringify(body),
        signal: controller.signal,
      });
    } catch (fetchError: unknown) {
      clearTimeout(timeout);
      // AbortError = timeout disparou
      if (fetchError instanceof Error && fetchError.name === 'AbortError') {
        log.warn('[API CHAT] Timeout de 90s atingido');
        return apiError('O serviço de IA demorou demais para responder. Tente novamente.', {
          request: req,
          status: 504,
        });
      }
      throw fetchError; // re-throw para cair no catch externo
    }

    clearTimeout(timeout);

    if (!response.ok) {
      log.warn('[API CHAT] Backend returned non-success', { status: response.status });
      return apiError('Erro no processamento da IA', {
        request: req,
        status: response.status,
      });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error: unknown) {
    log.error('[API CHAT] Erro no proxy', errorLogFields(error));
    return apiError('Falha interna ao conectar com o serviço de IA', {
      cause: error,
      logMessage: '[API CHAT] Proxy failed',
      request: req,
      status: 500,
    });
  }
}
