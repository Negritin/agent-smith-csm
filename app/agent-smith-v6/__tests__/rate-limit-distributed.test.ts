/**
 * MEDIO-004 — Rate limiter DISTRIBUIDO (Redis/Upstash).
 *
 * Cobre os criterios de aceite que exigem prova de comportamento:
 *  - Store distribuido via INCR + EXPIRE ATOMICO por chave (EVAL/Lua).
 *  - Limite GLOBAL ao cluster: duas "instancias" (dois clientes do store)
 *    compartilhando o MESMO backend somam o mesmo contador.
 *  - Indisponibilidade do store: fail-CLOSED em login/admin-login/reset/signup
 *    e fail-OPEN em forgot-password.
 *  - Paridade do store em memoria (fallback DEV).
 *
 * O Upstash REST e simulado por um servidor fake que emula o script Lua
 * (INCR + PEXPIRE so na primeira ocorrencia + PTTL), permitindo validar tanto a
 * atomicidade quanto o compartilhamento entre instancias sem rede real.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { rateLimit } from '@/lib/rate-limit';
import {
  MemoryRateLimitStore,
  RateLimitStoreError,
  UpstashRestRateLimitStore,
  __setRateLimitStoreForTests,
  type FetchLike,
  type RateLimitStore,
} from '@/lib/rate-limit-store';

// ---------------------------------------------------------------------------
// Fake Upstash REST server: um unico "Redis" compartilhado entre instancias.
// Emula o script Lua INCR_EXPIRE: INCR -> (count==1 ? PEXPIRE) -> PTTL.
// ---------------------------------------------------------------------------
class FakeUpstashServer {
  private readonly store = new Map<string, { count: number; expiresAt: number }>();
  /** Quantas vezes o PEXPIRE foi (re)aplicado por chave — prova de atomicidade. */
  public readonly expireCalls = new Map<string, number>();
  private clock = Date.now();

  advance(ms: number): void {
    this.clock += ms;
  }

  private bumpExpire(key: string): void {
    this.expireCalls.set(key, (this.expireCalls.get(key) ?? 0) + 1);
  }

  readonly fetch: FetchLike = async (_url, init) => {
    const args = JSON.parse(init?.body ?? '[]') as string[];
    const cmd = args[0];

    if (cmd === 'EVAL') {
      const key = args[3];
      const windowMs = Number(args[4]);
      const now = this.clock;

      let entry = this.store.get(key);
      if (!entry || entry.expiresAt <= now) {
        entry = { count: 0, expiresAt: now + windowMs };
      }
      entry.count += 1;
      if (entry.count === 1) {
        entry.expiresAt = now + windowMs; // PEXPIRE so na primeira ocorrencia
        this.bumpExpire(key);
      }
      this.store.set(key, entry);

      const pttl = entry.expiresAt - now;
      return okJson([entry.count, pttl]);
    }

    if (cmd === 'DEL') {
      this.store.delete(args[1]);
      return okJson(1);
    }

    return okJson(null);
  };
}

function okJson(result: unknown): Awaited<ReturnType<FetchLike>> {
  return {
    ok: true,
    status: 200,
    json: async () => ({ result }),
  };
}

afterEach(() => {
  __setRateLimitStoreForTests(null);
  vi.restoreAllMocks();
});

// ========================================================================== //
// Limite GLOBAL entre instancias (criterio: compartilhado entre instancias)
// ========================================================================== //
describe('MEDIO-004 — limite global multi-instancia (simulado)', () => {
  it('duas instancias compartilham o mesmo contador via store distribuido', async () => {
    const server = new FakeUpstashServer();
    // Duas "instancias" = dois clientes do store apontando para o MESMO backend.
    const instanceA = new UpstashRestRateLimitStore('https://fake', 'tok', server.fetch);
    const instanceB = new UpstashRestRateLimitStore('https://fake', 'tok', server.fetch);

    const key = 'login:ip:203.0.113.7';
    const max = 3;
    const windowMs = 60_000;

    // Instancia A consome 2 slots (counts 1, 2) — ambos liberados.
    __setRateLimitStoreForTests(instanceA);
    expect((await rateLimit(key, max, windowMs)).success).toBe(true); // count 1
    expect((await rateLimit(key, max, windowMs)).success).toBe(true); // count 2

    // Instancia B consome o 3o slot (count 3) — ainda dentro do limite.
    __setRateLimitStoreForTests(instanceB);
    const third = await rateLimit(key, max, windowMs);
    expect(third.success).toBe(true); // count 3
    expect(third.remaining).toBe(0);

    // 4a request (de volta na instancia A) estoura o limite GLOBAL.
    __setRateLimitStoreForTests(instanceA);
    const fourth = await rateLimit(key, max, windowMs);
    expect(fourth.success).toBe(false);
    expect(fourth.retryAfterSeconds).toBeGreaterThan(0);

    // Atomicidade: o EXPIRE foi aplicado UMA unica vez para a chave na janela.
    expect(server.expireCalls.get(key)).toBe(1);
  });

  it('reaplica o EXPIRE apos a janela expirar (nova janela)', async () => {
    const server = new FakeUpstashServer();
    const store = new UpstashRestRateLimitStore('https://fake', 'tok', server.fetch);
    __setRateLimitStoreForTests(store);

    const key = 'admin-login:ip:198.51.100.9';
    const windowMs = 1_000;

    await rateLimit(key, 5, windowMs); // count 1 -> expire #1
    await rateLimit(key, 5, windowMs); // count 2 -> sem expire
    expect(server.expireCalls.get(key)).toBe(1);

    server.advance(windowMs + 1); // janela expira
    const afterReset = await rateLimit(key, 5, windowMs); // count 1 -> expire #2
    expect(afterReset.success).toBe(true);
    expect(server.expireCalls.get(key)).toBe(2);
  });
});

// ========================================================================== //
// Indisponibilidade do store: fail-closed vs fail-open
// ========================================================================== //
describe('MEDIO-004 — politica de indisponibilidade do store', () => {
  const downStore: RateLimitStore = {
    kind: 'upstash',
    increment: async () => {
      throw new RateLimitStoreError('store indisponivel');
    },
    reset: async () => undefined,
  };

  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    __setRateLimitStoreForTests(downStore);
  });

  it('fail-CLOSED bloqueia login, admin-login, reset e signup', async () => {
    expect((await rateLimit('login:ip:x', 10, 1000)).success).toBe(false);
    expect((await rateLimit('admin-login:ip:x', 5, 1000)).success).toBe(false);
    expect((await rateLimit('reset:ip:x', 10, 1000)).success).toBe(false);
    expect((await rateLimit('signup:ip:x', 5, 1000)).success).toBe(false);
  });

  it('fail-OPEN libera forgot-password', async () => {
    const result = await rateLimit('forgot:ip:x', 5, 1000);
    expect(result.success).toBe(true);
    expect(result.remaining).toBe(4);
  });
});

// ========================================================================== //
// Paridade do store em memoria (fallback DEV)
// ========================================================================== //
describe('MEDIO-004 — store em memoria (fallback)', () => {
  it('respeita o limite por chave', async () => {
    __setRateLimitStoreForTests(new MemoryRateLimitStore());

    const key = 'login:ip:10.0.0.1';
    for (let i = 0; i < 10; i++) {
      expect((await rateLimit(key, 10, 60_000)).success).toBe(true);
    }
    expect((await rateLimit(key, 10, 60_000)).success).toBe(false);
  });
});
