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

// Força a rota a ser dinâmica para suportar streaming
export const dynamic = 'force-dynamic';

export async function POST(req: NextRequest) {
  try {
    // 1. Pega o corpo da requisição do frontend
    const body = await req.json();
    if (!body || typeof body !== 'object' || Array.isArray(body)) {
      return apiError('Requisição inválida', { request: req, status: 400 });
    }

    const requestedCompanyId = typeof body.companyId === 'string' ? body.companyId : null;
    const auth = await getUserOrAdminSession();
    if (auth.response) {
      return apiError('Não autorizado', { request: req, status: auth.response.status || 401 });
    }

    let targetCompanyId = auth.userSession?.companyId || auth.adminSession?.companyId || null;
    if (auth.adminSession?.role === 'master_admin' && requestedCompanyId) {
      targetCompanyId = requestedCompanyId;
      await auditMasterAdminCompanyOverride({
        request: req,
        actorId: auth.adminSession.adminId,
        sessionCompanyId: auth.adminSession.companyId || null,
        frontendCompanyId: requestedCompanyId,
        resourceType: 'chat',
        resourceId: requestedCompanyId,
        action: 'post_chat_stream',
      });
    }

    if (!targetCompanyId) {
      return apiError('Contexto de empresa obrigatório', { request: req, status: 403 });
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
        request: req,
        status: 'error',
        details: {
          attemptedAction: 'post_chat_stream',
        },
      });

      return apiError('Recurso não encontrado', { request: req, status: 404 });
    }

    const adminApiKeyResult = getAdminApiKeyOrResponse(req);
    if (adminApiKeyResult.response) return adminApiKeyResult.response;

    const internalAuthHeaders = auth.userSession
      ? createInternalAuthHeadersForUserSession(auth.userSession, targetCompanyId)
      : createInternalAuthHeadersForAdminSession(auth.adminSession, targetCompanyId);

    // 2. Define a URL do Backend (ajuste a porta se necessário, padrão 8000)
    const backendUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

    log.info('[PROXY] Connecting to backend chat stream');

    // 3. Faz a chamada ao Backend FastAPI
    const response = await fetch(`${backendUrl}/chat/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Admin-API-Key': adminApiKeyResult.adminApiKey,
        ...internalAuthHeaders,
      },
      body: JSON.stringify(body),
      // Propaga o abort do cliente (browser→Next) ao backend (Next→FastAPI):
      // quando o cliente desconecta no meio do stream, req.signal aborta este
      // fetch, fechando a conexão upstream → Starlette cancela o StreamingResponse
      // e o gerador SSE recebe CancelledError (não persiste parcial, libera o slot).
      signal: req.signal,
      // @ts-ignore - 'duplex' é necessário para streaming em algumas versões do Node
      duplex: 'half',
    });

    if (!response.ok) {
      log.warn('[PROXY] Backend stream returned non-success', { status: response.status });
      return apiError('Erro no processamento da IA', {
        request: req,
        status: response.status,
      });
    }

    if (!response.body) {
      return apiError('Resposta inválida do backend', {
        request: req,
        status: 502,
      });
    }

    // 4. Retorna a resposta como Stream para o cliente (Browser)
    return new NextResponse(response.body, {
      headers: {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        Connection: 'keep-alive',
      },
    });
  } catch (error: unknown) {
    log.error('[PROXY] Fatal stream error', errorLogFields(error));
    return apiError('Falha interna ao conectar com o serviço de IA', {
      cause: error,
      logMessage: '[PROXY] Stream proxy failed',
      request: req,
      status: 500,
    });
  }
}
