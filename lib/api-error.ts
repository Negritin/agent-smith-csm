import { NextResponse } from 'next/server';
import { log } from './logger';

type ApiErrorOptions = {
  cause?: unknown;
  correlationId?: string;
  headers?: HeadersInit;
  logContext?: Record<string, unknown>;
  logMessage?: string;
  request?: Request;
  status?: number;
};

function createCorrelationId(): string {
  return crypto.randomUUID();
}

function getCorrelationId(request?: Request, fallback?: string): string {
  return (
    request?.headers.get('x-correlation-id') ||
    request?.headers.get('x-request-id') ||
    fallback ||
    createCorrelationId()
  );
}

function errorLogContext(cause: unknown): Record<string, unknown> {
  if (cause instanceof Error) {
    return {
      errorName: cause.name,
    };
  }

  return {
    errorType: typeof cause,
  };
}

export function apiError(
  error: string,
  {
    cause,
    correlationId: providedCorrelationId,
    headers,
    logContext,
    logMessage,
    request,
    status = 500,
  }: ApiErrorOptions = {},
): NextResponse {
  const correlationId = getCorrelationId(request, providedCorrelationId);

  if (cause || logMessage) {
    log.error(logMessage || '[API] Request failed', {
      ...(cause ? errorLogContext(cause) : {}),
      ...logContext,
      correlationId,
      status,
    });
  }

  const responseHeaders = new Headers(headers);
  responseHeaders.set('x-correlation-id', correlationId);

  return NextResponse.json(
    {
      error,
      correlationId,
    },
    {
      status,
      headers: responseHeaders,
    },
  );
}

export async function authApiError(
  authResponse: NextResponse,
  options: { fallback?: string; request?: Request } = {},
): Promise<NextResponse> {
  let error = options.fallback || 'Não autorizado';

  try {
    const body: unknown = await authResponse.clone().json();
    if (body && typeof body === 'object' && !Array.isArray(body)) {
      const record = body as Record<string, unknown>;
      if (typeof record.error === 'string' && record.error) {
        error = record.error;
      }
    }
  } catch {
    // Keep the fallback message when the original auth response is not JSON.
  }

  const headers = new Headers(authResponse.headers);
  headers.delete('content-length');

  return apiError(error, {
    headers,
    request: options.request,
    status: authResponse.status || 401,
  });
}

export function upstreamApiError(
  status: number,
  options: Omit<ApiErrorOptions, 'status'> & { fallback?: string } = {},
): NextResponse {
  return apiError(options.fallback || 'Erro ao conectar com o backend', {
    ...options,
    status,
  });
}
