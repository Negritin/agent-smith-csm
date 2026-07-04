import { NextRequest, NextResponse } from 'next/server';
import { apiError } from '@/lib/api-error';
import { getAdminApiKeyOrResponse } from '@/lib/admin-proxy';
import { errorLogFields, log } from '@/lib/logger';

// Força a rota a ser dinâmica para suportar streaming.
export const dynamic = 'force-dynamic';

/**
 * POST /api/widget/chat/stream
 *
 * Proxy SSE público para o backend (/chat/stream), autenticado por WIDGET TOKEN
 * (NÃO por sessão user/admin — um lead anônimo não tem sessão). Espelha
 * app/api/chat/stream/route.ts (force-dynamic, fetch duplex:'half', resposta como
 * stream text/event-stream), mas troca o bloco getUserOrAdminSession pela
 * validação do widget token, exatamente como o caminho de widget em
 * app/api/chat/route.ts (getTrustedChatHeaders/isWidgetChatBody). Importante: este
 * arquivo NUNCA importa/chama getUserOrAdminSession.
 */
export async function POST(req: NextRequest) {
  try {
    // 1) Auth de widget: o token curto (HMAC) é minted após validação de origem no
    // bootstrap. Sem ele → 401 imediato, sem nunca tocar a sessão user/admin.
    const widgetToken = req.headers.get('x-widget-token');
    if (!widgetToken) {
      return apiError('Widget token obrigatório', { request: req, status: 401 });
    }

    // 2) Corpo da requisição (mesmo body do widget, channel:'widget').
    const body = await req.json();
    if (!body || typeof body !== 'object' || Array.isArray(body)) {
      return apiError('Requisição inválida', { request: req, status: 400 });
    }

    // 3) Admin API key server-to-server (idêntico ao proxy de stream atual).
    const adminApiKeyResult = getAdminApiKeyOrResponse(req);
    if (adminApiKeyResult.response) return adminApiKeyResult.response;

    const backendUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

    log.info('[PROXY] Connecting to backend widget chat stream');

    // 4) Chamada ao backend FastAPI repassando X-Widget-Token + X-Admin-API-Key.
    const response = await fetch(`${backendUrl}/chat/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Admin-API-Key': adminApiKeyResult.adminApiKey,
        'X-Widget-Token': widgetToken,
      },
      body: JSON.stringify(body),
      // Propaga o abort do cliente (browser→Next) ao backend (Next→FastAPI): quando
      // o lead fecha o widget no meio do stream, req.signal aborta este fetch,
      // fechando a conexão upstream → Starlette cancela o StreamingResponse e o
      // gerador SSE recebe CancelledError (não persiste parcial, libera o slot).
      signal: req.signal,
      // @ts-ignore - 'duplex' é necessário para streaming em algumas versões do Node
      duplex: 'half',
    });

    if (!response.ok) {
      log.warn('[PROXY] Backend widget stream returned non-success', {
        status: response.status,
      });
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

    // 5) Retorna a resposta como stream para o cliente (Browser).
    return new NextResponse(response.body, {
      headers: {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        Connection: 'keep-alive',
      },
    });
  } catch (error: unknown) {
    log.error('[PROXY] Fatal widget stream error', errorLogFields(error));
    return apiError('Falha interna ao conectar com o serviço de IA', {
      cause: error,
      logMessage: '[PROXY] Widget stream proxy failed',
      request: req,
      status: 500,
    });
  }
}
