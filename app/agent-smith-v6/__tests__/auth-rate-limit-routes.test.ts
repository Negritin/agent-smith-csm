/**
 * Sprint "Rate limiting e lockout de autenticacao" — testes de regressao de ROTA.
 *
 * Cobre os criterios de aceite que exigem teste de ENDPOINT (HTTP 429):
 *  - ALTO-001 : /api/admin/login limita por IP (5/15min) e por e-mail (5/15min).
 *  - MEDIO-002: /api/auth/login limita por IP (RATE_LIMITS.LOGIN_IP = 10/15min).
 *  - MEDIO-003: /api/auth/signup limita por IP (5/hora).
 *
 * O store de rate-limit (lib/rate-limit.ts) e um Map em memoria do modulo, REAL e
 * compartilhado dentro deste arquivo. Para evitar contaminacao entre os casos,
 * cada teste usa um IP/e-mail UNICO. As funcoes de auth (loginAdmin/loginUser/
 * createUser) sao mockadas — o gate de rate limit ocorre ANTES delas, entao para o
 * caso 429 elas nem sao chamadas; nos demais devolvem erro para forcar o 401/400.
 */
import { describe, expect, it, vi } from 'vitest';

// Auth: stubs (o rate limit roda antes; no 429 nem chega aqui).
vi.mock('@/lib/auth', () => ({
  loginAdmin: vi.fn(async () => ({ admin: null, error: 'Email ou senha incorretos' })),
  loginUser: vi.fn(async () => ({ user: null, company: null, error: 'Email ou senha incorretos' })),
  createUser: vi.fn(async () => ({ user: null, error: 'erro' })),
  validatePasswordStrength: () => ({ valid: true, errors: [] }),
}));

// Efeitos colaterais de I/O fora do escopo do gate de rate limit.
vi.mock('@/lib/logger', async (orig) => ({
  ...(await orig<typeof import('@/lib/logger')>()),
  logSystemAction: vi.fn(async () => undefined),
}));
vi.mock('@/lib/security-audit', () => ({ logSecurityAudit: vi.fn(async () => undefined) }));
vi.mock('@/lib/auth-actions', () => ({
  saveAdminSession: vi.fn(async () => undefined),
  saveUserSession: vi.fn(async () => undefined),
}));
vi.mock('@/lib/supabase-admin', () => ({ getSupabaseAdmin: () => ({ from: () => ({}) }) }));

function post(url: string, body: unknown, ip: string): Request {
  return new Request(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'x-forwarded-for': ip },
    body: JSON.stringify(body),
  });
}

// ========================================================================== //
// ALTO-001 — /api/admin/login
// ========================================================================== //
describe('ALTO-001 /api/admin/login — rate limit', () => {
  it('IP excedente (6a request, 5/15min) recebe 429', async () => {
    const { POST } = await import('@/app/api/admin/login/route');
    const ip = '203.0.113.10';

    // 5 requests com e-mails DISTINTOS (so o limite de IP deve estourar).
    for (let i = 0; i < 5; i++) {
      const res = await POST(
        post('http://t/api/admin/login', { email: `a${i}@x.com`, password: 'x' }, ip) as never,
      );
      expect(res.status).toBe(401);
    }

    const sixth = await POST(
      post('http://t/api/admin/login', { email: 'a6@x.com', password: 'x' }, ip) as never,
    );
    expect(sixth.status).toBe(429);
  });

  it('e-mail excedente (6a request, 5/15min) recebe 429', async () => {
    const { POST } = await import('@/app/api/admin/login/route');
    const email = 'target@x.com';

    // 5 requests com IPs DISTINTOS (so o limite por e-mail deve estourar).
    for (let i = 0; i < 5; i++) {
      const res = await POST(
        post('http://t/api/admin/login', { email, password: 'x' }, `198.51.100.${i}`) as never,
      );
      expect(res.status).toBe(401);
    }

    const sixth = await POST(
      post('http://t/api/admin/login', { email, password: 'x' }, '198.51.100.200') as never,
    );
    expect(sixth.status).toBe(429);
  });
});

// ========================================================================== //
// MEDIO-002 — /api/auth/login
// ========================================================================== //
describe('MEDIO-002 /api/auth/login — rate limit por IP', () => {
  it('IP excedente (11a request, LOGIN_IP 10/15min) recebe 429', async () => {
    const { POST } = await import('@/app/api/auth/login/route');
    const ip = '203.0.113.50';

    // 10 requests com e-mails DISTINTOS (so o limite de IP deve estourar).
    for (let i = 0; i < 10; i++) {
      const res = await POST(
        post('http://t/api/auth/login', { email: `u${i}@x.com`, password: 'x' }, ip) as never,
      );
      expect(res.status).toBe(401);
    }

    const eleventh = await POST(
      post('http://t/api/auth/login', { email: 'u11@x.com', password: 'x' }, ip) as never,
    );
    expect(eleventh.status).toBe(429);
  });
});

// ========================================================================== //
// MEDIO-003 — /api/auth/signup
// ========================================================================== //
describe('MEDIO-003 /api/auth/signup — rate limit por IP', () => {
  it('IP excedente (6a request, 5/hora) recebe 429', async () => {
    const { POST } = await import('@/app/api/auth/signup/route');
    const ip = '203.0.113.90';

    // 5 requests (corpo invalido -> 400, mas contam no rate limit, que roda antes).
    for (let i = 0; i < 5; i++) {
      const res = await POST(post('http://t/api/auth/signup', {}, ip) as never);
      expect(res.status).toBe(400);
    }

    const sixth = await POST(post('http://t/api/auth/signup', {}, ip) as never);
    expect(sixth.status).toBe(429);
  });
});
