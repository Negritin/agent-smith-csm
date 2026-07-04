/**
 * Rate Limit Store — backend abstrato + implementacoes.
 *
 * Objetivo (MEDIO-004): mover o rate limiter de um Map em processo para um store
 * DISTRIBUIDO compartilhado entre instancias (replicas), evitando que o limite
 * efetivo seja multiplicado pelo numero de processos.
 *
 * Contrato do store:
 *  - `increment(key, windowMs)` faz um INCR + EXPIRE ATOMICO por chave e devolve
 *    a contagem global atual e o instante de reset (epoch ms).
 *  - A atomicidade e garantida no backend distribuido (Upstash) por um unico
 *    script Lua (EVAL) — INCR e PEXPIRE rodam na mesma operacao, sem janela de
 *    corrida entre replicas.
 *
 * Selecao de backend (getRateLimitStore):
 *  - Se UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN estiverem setados,
 *    usa o store distribuido (Upstash REST, atomico, compartilhado).
 *  - Caso contrario, cai no store em memoria (apenas DEV/local — NAO e
 *    compartilhado entre instancias). Em producao multi-replica, configure o
 *    Upstash para que os limites sejam globais ao cluster.
 *
 * Observacao: usamos a API REST do Upstash via `fetch` (zero dependencias novas),
 * compativel com o runtime do Next.js. REDIS_URL (protocolo RESP/TCP) e usado
 * pelo backend Python; o limiter do front usa o endpoint REST do Upstash.
 */

export interface RateLimitIncrementResult {
  /** Contagem global atual da chave dentro da janela. */
  count: number;
  /** Instante (epoch ms) em que a janela expira/reseta. */
  resetAt: number;
}

export interface RateLimitStore {
  /** Identifica o backend ativo (util para logs/observabilidade e testes). */
  readonly kind: 'memory' | 'upstash';
  /**
   * Incrementa atomicamente o contador da chave, garantindo que o TTL/expire
   * seja aplicado na primeira ocorrencia da janela. Pode lancar em caso de
   * indisponibilidade do backend (a politica fail-open/closed e decidida pelo
   * chamador, em lib/rate-limit.ts).
   */
  increment(key: string, windowMs: number): Promise<RateLimitIncrementResult>;
  /** Zera o contador da chave (best-effort). */
  reset(key: string): Promise<void>;
}

/** Erro tipado para falhas de comunicacao/operacao com o store distribuido. */
export class RateLimitStoreError extends Error {
  constructor(
    message: string,
    public readonly cause?: unknown,
  ) {
    super(message);
    this.name = 'RateLimitStoreError';
  }
}

// ==========================================================================
// Store em memoria (DEV/local / fallback) — sliding fixed-window por chave.
// NAO e compartilhado entre instancias; mantido por paridade com o
// comportamento legado e para ambientes sem Upstash configurado.
// ==========================================================================

interface MemoryRecord {
  count: number;
  resetAt: number;
}

export class MemoryRateLimitStore implements RateLimitStore {
  public readonly kind = 'memory' as const;
  private readonly store = new Map<string, MemoryRecord>();

  constructor() {
    // Limpeza periodica de entradas expiradas. `unref` evita segurar o event
    // loop vivo (importante para encerramento de testes/processos).
    const timer = setInterval(() => this.cleanup(), 60_000);
    if (typeof timer === 'object' && typeof timer.unref === 'function') {
      timer.unref();
    }
  }

  private cleanup(): void {
    const now = Date.now();
    const expired: string[] = [];
    this.store.forEach((record, key) => {
      if (record.resetAt < now) {
        expired.push(key);
      }
    });
    expired.forEach((key) => this.store.delete(key));
  }

  async increment(key: string, windowMs: number): Promise<RateLimitIncrementResult> {
    const now = Date.now();
    const record = this.store.get(key);

    if (!record || record.resetAt < now) {
      const resetAt = now + windowMs;
      this.store.set(key, { count: 1, resetAt });
      return { count: 1, resetAt };
    }

    record.count += 1;
    return { count: record.count, resetAt: record.resetAt };
  }

  async reset(key: string): Promise<void> {
    this.store.delete(key);
  }
}

// ==========================================================================
// Store distribuido (Upstash REST) — INCR + PEXPIRE atomico via EVAL (Lua).
// ==========================================================================

/** Assinatura minima de `fetch` usada pelo store (injetavel em testes). */
export type FetchLike = (
  input: string,
  init?: {
    method?: string;
    headers?: Record<string, string>;
    body?: string;
  },
) => Promise<{
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
}>;

/**
 * Script Lua executado atomicamente no Redis:
 *  1. INCR da chave;
 *  2. Na PRIMEIRA ocorrencia da janela (count == 1), aplica PEXPIRE(windowMs);
 *  3. Retorna [count, pttl] para o chamador derivar `resetAt`.
 *
 * Garante que INCR e EXPIRE nunca fiquem dessincronizados entre replicas.
 */
const INCR_EXPIRE_LUA = `
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('PEXPIRE', KEYS[1], ARGV[1])
end
return {count, redis.call('PTTL', KEYS[1])}
`.trim();

interface UpstashCommandResponse {
  result?: unknown;
  error?: string;
}

export class UpstashRestRateLimitStore implements RateLimitStore {
  public readonly kind = 'upstash' as const;

  constructor(
    private readonly url: string,
    private readonly token: string,
    private readonly fetchImpl: FetchLike = defaultFetch,
  ) {}

  private async command(args: (string | number)[]): Promise<unknown> {
    let response: Awaited<ReturnType<FetchLike>>;
    try {
      response = await this.fetchImpl(this.url, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${this.token}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(args.map(String)),
      });
    } catch (err) {
      throw new RateLimitStoreError('Falha de rede ao contatar o Upstash', err);
    }

    if (!response.ok) {
      throw new RateLimitStoreError(`Upstash respondeu HTTP ${response.status}`);
    }

    const data = (await response.json()) as UpstashCommandResponse;
    if (data && typeof data === 'object' && 'error' in data && data.error) {
      throw new RateLimitStoreError(`Upstash retornou erro: ${data.error}`);
    }
    return data?.result;
  }

  async increment(key: string, windowMs: number): Promise<RateLimitIncrementResult> {
    const result = await this.command(['EVAL', INCR_EXPIRE_LUA, '1', key, windowMs]);

    if (!Array.isArray(result) || result.length < 2) {
      throw new RateLimitStoreError('Resposta inesperada do Upstash para EVAL');
    }

    const count = Number(result[0]);
    const pttl = Number(result[1]);
    if (!Number.isFinite(count)) {
      throw new RateLimitStoreError('Contagem invalida retornada pelo Upstash');
    }

    // pttl < 0 significa chave sem expire (-1) ou inexistente (-2); usamos a
    // janela completa como fallback defensivo.
    const resetAt = Date.now() + (pttl > 0 ? pttl : windowMs);
    return { count, resetAt };
  }

  async reset(key: string): Promise<void> {
    await this.command(['DEL', key]);
  }
}

const defaultFetch: FetchLike = (input, init) => {
  if (typeof fetch !== 'function') {
    return Promise.reject(new RateLimitStoreError('global fetch indisponivel neste runtime'));
  }
  return fetch(input, init as RequestInit) as unknown as ReturnType<FetchLike>;
};

// ==========================================================================
// Selecao e cache do store ativo.
// ==========================================================================

let cachedStore: RateLimitStore | null = null;

export function getRateLimitStore(): RateLimitStore {
  if (cachedStore) {
    return cachedStore;
  }

  const url = process.env.UPSTASH_REDIS_REST_URL;
  const token = process.env.UPSTASH_REDIS_REST_TOKEN;

  if (url && token) {
    cachedStore = new UpstashRestRateLimitStore(url, token);
  } else {
    cachedStore = new MemoryRateLimitStore();
  }

  return cachedStore;
}

/** Override do store ativo — uso exclusivo de testes. */
export function __setRateLimitStoreForTests(store: RateLimitStore | null): void {
  cachedStore = store;
}
