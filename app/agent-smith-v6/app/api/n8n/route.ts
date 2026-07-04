import { NextRequest, NextResponse } from 'next/server';
import { cookies } from 'next/headers';
import { getIronSession } from 'iron-session';
import { createClient } from '@supabase/supabase-js';
import { apiError } from '@/lib/api-error';
import { errorLogFields, getClientInfo, log } from '@/lib/logger';
import { sessionOptions, SessionData } from '@/lib/iron-session';
import { getAdminApiKeyOrResponse } from '@/lib/admin-proxy';
import { createInternalAuthHeadersForUserSession } from '@/lib/internal-jwt';
import {
  ExternalUrlValidationError,
  MAX_EXTERNAL_RESPONSE_BYTES,
  revalidateExternalUrl,
  ValidatedExternalUrl,
  validateExternalUrl,
} from '@/lib/security/url-validator';

export const dynamic = 'force-dynamic';

// Service Role Client
const supabaseAdmin = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY!,
  { auth: { persistSession: false } },
);

const N8N_TIMEOUT_MS = 30_000;

class UpstreamResponseTooLargeError extends Error {
  constructor() {
    super('upstream_response_too_large');
    this.name = 'UpstreamResponseTooLargeError';
  }
}

async function readResponseBodyWithLimit(
  response: Response,
  controller: AbortController,
): Promise<string> {
  if (!response.body) {
    const text = await response.text();
    if (new TextEncoder().encode(text).byteLength > MAX_EXTERNAL_RESPONSE_BYTES) {
      throw new UpstreamResponseTooLargeError();
    }
    return text;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let totalBytes = 0;
  let body = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    totalBytes += value.byteLength;
    if (totalBytes > MAX_EXTERNAL_RESPONSE_BYTES) {
      controller.abort();
      throw new UpstreamResponseTooLargeError();
    }

    body += decoder.decode(value, { stream: true });
  }

  return body + decoder.decode();
}

export async function POST(request: NextRequest) {
  const { ipAddress, userAgent } = getClientInfo(request);
  log.info('[N8N API] Request received', { ip: ipAddress, userAgent });

  try {
    // Ler sessão do cookie criptografado
    const cookieStore = await cookies();
    const session = await getIronSession<SessionData>(cookieStore, sessionOptions);

    const body = await request.json();

    const targetCompanyId = session.companyId;

    if (!session.userId || !targetCompanyId) {
      log.warn('[N8N API] Missing company in session', { ip: ipAddress, userAgent });
      return apiError('Não autorizado', { request, status: 401 });
    }

    log.info('[N8N API] Using session company', { companyId: targetCompanyId });

    // Buscar informações da company
    const { data: company } = await supabaseAdmin
      .from('companies')
      .select('webhook_url, use_langchain')
      .eq('id', targetCompanyId)
      .maybeSingle();

    if (!company) {
      log.warn('[N8N API] Company not found', { companyId: targetCompanyId });
      return apiError('Empresa não encontrada', { request, status: 400 });
    }

    const useLangChain = company.use_langchain || false;

    let targetUrl: string;
    let targetType: string;

    if (useLangChain) {
      targetUrl = process.env.NEXT_PUBLIC_LANGCHAIN_API_URL || 'http://localhost:8000/chat';
      targetType = 'LangChain (FastAPI)';
    } else {
      const webhookUrl = company.webhook_url;
      if (!webhookUrl) {
        log.warn('[N8N API] Webhook not configured', { companyId: targetCompanyId });
        return apiError('Webhook não configurado', { request, status: 400 });
      }
      targetUrl = webhookUrl;
      targetType = 'N8N Webhook';
    }

    log.info('[N8N API] Routing request', { targetType, companyId: targetCompanyId });

    // Payload final para o Backend/N8N
    const enrichedBody = {
      ...body,
      companyId: targetCompanyId, // Ignora qualquer companyId enviado pelo cliente
      userId: session.userId, // Ignora userId enviado pelo cliente
    };

    let validatedTargetUrl: ValidatedExternalUrl | null = null;
    if (!useLangChain) {
      validatedTargetUrl = await validateExternalUrl(targetUrl);
    }
    const langChainHeaders = useLangChain
      ? createInternalAuthHeadersForUserSession(session, targetCompanyId)
      : null;
    const adminApiKeyResult = useLangChain ? getAdminApiKeyOrResponse(request) : null;
    if (adminApiKeyResult?.response) return adminApiKeyResult.response;

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), N8N_TIMEOUT_MS);
    let apiResponse: Response;
    let responseText: string;

    try {
      if (validatedTargetUrl) {
        await revalidateExternalUrl(validatedTargetUrl);
      }

      apiResponse = await fetch(targetUrl, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(useLangChain && adminApiKeyResult && langChainHeaders
            ? {
              'X-Admin-API-Key': adminApiKeyResult.adminApiKey,
              Authorization: langChainHeaders.Authorization,
            }
            : {}),
        },
        body: JSON.stringify(enrichedBody),
        redirect: 'manual',
        signal: controller.signal,
      });

      responseText = await readResponseBodyWithLimit(apiResponse, controller);
    } finally {
      clearTimeout(timeout);
    }

    if (!apiResponse.ok) {
      log.warn('[N8N API] Upstream returned non-success', {
        status: apiResponse.status,
        targetType,
      });
      return apiError('Upstream Error', { request, status: apiResponse.status });
    }

    const responseData = responseText ? JSON.parse(responseText) : {};
    return NextResponse.json(responseData);
  } catch (error: unknown) {
    log.error('[N8N API] Request failed', {
      ...errorLogFields(error),
      ip: ipAddress,
      userAgent,
    });

    if (error instanceof ExternalUrlValidationError) {
      return apiError('Webhook inválido', { request, status: 400 });
    }

    if (error instanceof UpstreamResponseTooLargeError) {
      return apiError('Upstream response too large', { request, status: 502 });
    }

    if (error instanceof Error && error.name === 'AbortError') {
      return apiError('Upstream timeout', { request, status: 504 });
    }

    return apiError('Internal Server Error', {
      cause: error,
      logMessage: '[N8N API] Unhandled request failure',
      request,
      status: 500,
    });
  }
}
