/**
 * Rate Limiting Utility
 *
 * Limiter DISTRIBUIDO (Redis/Upstash) compartilhado entre instancias.
 *
 * O backend e abstraido em lib/rate-limit-store.ts:
 *  - Upstash REST (atomico via INCR + PEXPIRE em um unico EVAL) quando
 *    UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN estao configurados;
 *  - Memoria em processo (DEV/local) como fallback.
 *
 * ----------------------------------------------------------------------------
 * ASSINATURA PUBLICA (preservada): rateLimit(key, maxRequests, windowMs)
 * ----------------------------------------------------------------------------
 * A lista de parametros e o shape de RateLimitResult sao mantidos para os
 * consumidores (forgot/reset/login/admin-login/signup). A unica diferenca em
 * relacao ao limiter legado e que a funcao agora e ASSINCRONA (retorna
 * Promise<RateLimitResult>): operacoes atomicas em um store distribuido
 * (Redis/Upstash) sao inerentemente assincronas — nao ha como manter um
 * INCR+EXPIRE atomico e global de forma sincrona. Os call sites apenas
 * adicionam `await` antes da chamada; o uso de `.success/.remaining/...`
 * permanece identico.
 *
 * ----------------------------------------------------------------------------
 * POLITICA DE INDISPONIBILIDADE DO STORE (DECIDIDA — MEDIO-004)
 * ----------------------------------------------------------------------------
 * Quando o store distribuido esta indisponivel (erro de rede/HTTP), a decisao
 * fail-open vs fail-closed e derivada do PREFIXO da chave, mantendo a
 * assinatura publica intacta:
 *
 *   - fail-CLOSED  -> login, admin-login, reset-password, signup
 *                     (bloqueia: o limiter "fecha" sob falha para proteger
 *                      endpoints sensiveis contra brute-force/credential
 *                      stuffing quando nao conseguimos contar globalmente).
 *
 *   - fail-OPEN    -> forgot-password
 *                     (libera: enviar e-mail de recuperacao e disponibilidade
 *                      de baixo risco de abuso e nao deve ficar indisponivel
 *                      por uma falha do store; o pior caso e e-mail extra).
 *
 * Qualquer prefixo nao mapeado e tratado como fail-CLOSED (default seguro).
 */

import { getRateLimitStore } from './rate-limit-store';

export interface RateLimitResult {
  success: boolean;
  remaining: number;
  resetAt: number;
  retryAfterSeconds: number;
}

/** Decide a politica de indisponibilidade a partir do prefixo da chave. */
type FailPolicy = 'open' | 'closed';

function failPolicyForKey(key: string): FailPolicy {
  // fail-OPEN APENAS para forgot-password; todo o resto e fail-CLOSED.
  return key.startsWith('forgot:') ? 'open' : 'closed';
}

/**
 * Check rate limit for a given key.
 *
 * @param key - Unique identifier (e.g., IP address, email, token). O PREFIXO da
 *   chave define a politica fail-open/closed sob indisponibilidade do store.
 * @param maxRequests - Maximum requests allowed in window.
 * @param windowMs - Time window in milliseconds.
 */
export async function rateLimit(
  key: string,
  maxRequests: number,
  windowMs: number,
): Promise<RateLimitResult> {
  const store = getRateLimitStore();

  try {
    const { count, resetAt } = await store.increment(key, windowMs);

    if (count > maxRequests) {
      const retryAfterSeconds = Math.max(0, Math.ceil((resetAt - Date.now()) / 1000));
      return { success: false, remaining: 0, resetAt, retryAfterSeconds };
    }

    return {
      success: true,
      remaining: Math.max(0, maxRequests - count),
      resetAt,
      retryAfterSeconds: 0,
    };
  } catch (error) {
    return handleStoreUnavailable(key, maxRequests, windowMs, error);
  }
}

/**
 * Aplica a politica fail-open/closed quando o store distribuido falha.
 * Veja o bloco de documentacao no topo do arquivo.
 */
function handleStoreUnavailable(
  key: string,
  maxRequests: number,
  windowMs: number,
  error: unknown,
): RateLimitResult {
  const policy = failPolicyForKey(key);
  const now = Date.now();
  const resetAt = now + windowMs;

  // Log sem expor PII: registramos apenas o prefixo da chave (ex.: "login").
  const keyPrefix = key.split(':')[0] ?? 'unknown';
  console.error('[RATE LIMIT] Store indisponivel — aplicando fail-%s', policy, {
    keyPrefix,
    errorName: error instanceof Error ? error.name : typeof error,
  });

  if (policy === 'open') {
    // Libera a request, mas sinaliza o consumo "otimista" de 1 slot.
    return {
      success: true,
      remaining: Math.max(0, maxRequests - 1),
      resetAt,
      retryAfterSeconds: 0,
    };
  }

  // fail-CLOSED: bloqueia para proteger endpoints sensiveis.
  return {
    success: false,
    remaining: 0,
    resetAt,
    retryAfterSeconds: Math.ceil(windowMs / 1000),
  };
}

/**
 * Reset rate limit for a key (e.g., after successful action).
 * Best-effort: falhas do store sao absorvidas (nao devem quebrar o fluxo).
 */
export async function resetRateLimit(key: string): Promise<void> {
  try {
    await getRateLimitStore().reset(key);
  } catch (error) {
    console.error('[RATE LIMIT] Falha ao resetar chave', {
      keyPrefix: key.split(':')[0] ?? 'unknown',
      errorName: error instanceof Error ? error.name : typeof error,
    });
  }
}

/**
 * Get rate limit headers for HTTP response.
 */
export function getRateLimitHeaders(result: RateLimitResult): Record<string, string> {
  return {
    'X-RateLimit-Remaining': result.remaining.toString(),
    'X-RateLimit-Reset': new Date(result.resetAt).toISOString(),
    ...(result.retryAfterSeconds > 0 && {
      'Retry-After': result.retryAfterSeconds.toString(),
    }),
  };
}

// Common rate limit configurations
export const RATE_LIMITS = {
  FORGOT_PASSWORD_IP: { maxRequests: 5, windowMs: 60 * 60 * 1000 }, // 5/hour per IP
  FORGOT_PASSWORD_EMAIL: { maxRequests: 3, windowMs: 60 * 60 * 1000 }, // 3/hour per email
  RESET_PASSWORD_IP: { maxRequests: 10, windowMs: 60 * 60 * 1000 }, // 10/hour per IP
  RESET_PASSWORD_TOKEN: { maxRequests: 5, windowMs: 60 * 60 * 1000 }, // 5 attempts per token
  LOGIN_IP: { maxRequests: 10, windowMs: 15 * 60 * 1000 }, // 10/15min per IP (user login)
  LOGIN_EMAIL: { maxRequests: 5, windowMs: 15 * 60 * 1000 }, // 5/15min per email (user login)
  ADMIN_LOGIN_IP: { maxRequests: 5, windowMs: 15 * 60 * 1000 }, // 5/15min per IP (admin login)
  ADMIN_LOGIN_EMAIL: { maxRequests: 5, windowMs: 15 * 60 * 1000 }, // 5/15min per email (admin login)
  SIGNUP_IP: { maxRequests: 5, windowMs: 60 * 60 * 1000 }, // 5/hour per IP (signup)
} as const;
