/**
 * Sprint "Rate limiting e lockout de autenticacao" — testes de regressao da
 * camada lib/auth.ts.
 *
 * Cobre:
 *  - ALTO-001: loginAdmin implementa lockout progressivo (espelhando loginUser):
 *    5 falhas bloqueiam a conta por 15 min; a 6a tentativa cai no ramo de conta
 *    bloqueada e retorna erro GENERICO (sem revelar o bloqueio); o contador NAO
 *    incrementa quando ja bloqueado. Cobre o caminho master_admin (admin_users) e
 *    o caminho company_admin (users_v2).
 *  - MEDIO-003: createUser retorna a MESMA mensagem generica (nao-diferencial)
 *    para duplicidade de email e de CPF (anti-enumeracao de PII).
 *
 * lib/auth.ts cria o client via createClient(@supabase/supabase-js) em import-time,
 * entao mockamos esse modulo com um fake STATEFUL: leituras com `liveRows` devolvem
 * a MESMA referencia (updates via Object.assign persistem entre chamadas);
 * leituras com `reads` consomem uma fila por tabela.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

// --- Fake stateful do supabase-js -------------------------------------------
type ReadResult = { data: unknown; error?: unknown };
interface FakeConfig {
  liveRows: Record<string, Record<string, unknown>>;
  reads: Record<string, ReadResult[]>;
  updates: Array<{ table: string; values: Record<string, unknown> }>;
}

let cfg: FakeConfig;

function resetConfig(): void {
  cfg = { liveRows: {}, reads: {}, updates: [] };
}
resetConfig();

function makeBuilder(table: string) {
  let pendingUpdate: Record<string, unknown> | null = null;

  const resolveRead = (): ReadResult => {
    if (cfg.liveRows[table]) {
      return { data: cfg.liveRows[table], error: null };
    }
    const queue = cfg.reads[table] ?? [];
    const next = queue.shift() ?? { data: null, error: null };
    return { data: next.data, error: next.error ?? null };
  };

  const terminal = (): ReadResult => {
    if (pendingUpdate) {
      cfg.updates.push({ table, values: pendingUpdate });
      if (cfg.liveRows[table]) {
        Object.assign(cfg.liveRows[table], pendingUpdate);
      }
      return { data: [{}], error: null };
    }
    return resolveRead();
  };

  const builder: Record<string, unknown> = {
    select: () => builder,
    update: (values: Record<string, unknown>) => {
      pendingUpdate = values;
      return builder;
    },
    insert: () => builder,
    upsert: () => builder,
    delete: () => builder,
    eq: () => builder,
    neq: () => builder,
    ilike: () => builder,
    in: () => builder,
    is: () => builder,
    order: () => builder,
    limit: () => builder,
    maybeSingle: async () => terminal(),
    single: async () => terminal(),
    then: (onF: (v: unknown) => unknown, onR?: (e: unknown) => unknown) =>
      Promise.resolve(terminal()).then(onF, onR),
  };
  return builder;
}

const fakeClient = {
  from: (table: string) => makeBuilder(table),
  rpc: async () => ({ data: null, error: null }),
};

// O factory do vi.mock e HOISTADO acima das declaracoes; createClient e chamado
// no import de lib/auth (antes de `fakeClient` inicializar). Retornamos um Proxy
// que resolve `fakeClient` LAZY (no acesso a `.from`/`.rpc`, ja apos a init).
vi.mock('@supabase/supabase-js', () => ({
  createClient: () =>
    new Proxy(
      {},
      {
        get(_t, prop) {
          const value = (fakeClient as Record<string | symbol, unknown>)[prop];
          return typeof value === 'function' ? value.bind(fakeClient) : value;
        },
      },
    ),
}));

// Import APOS o mock para que lib/auth receba o fake.
import { loginAdmin, createUser, DUPLICATE_SIGNUP_ERROR, type SignupData } from '@/lib/auth';

const INVALID_HASH = 'a'.repeat(64); // SHA-256 legacy-like; nunca casa com a senha.
const VALID_CPF = '11144477735'; // CPF valido (algoritmo de digitos verificadores).

beforeEach(() => {
  resetConfig();
});

// ========================================================================== //
// ALTO-001 — loginAdmin lockout (master_admin / admin_users)
// ========================================================================== //
describe('ALTO-001 loginAdmin lockout — master_admin (admin_users)', () => {
  it('5 falhas bloqueiam a conta; 6a tentativa = conta bloqueada (erro generico)', async () => {
    const masterRow = {
      id: 'm1',
      email: 'master@x.com',
      role: 'master_admin',
      password_hash: INVALID_HASH,
      failed_login_attempts: 0,
      account_locked_until: null as string | null,
    };
    cfg.liveRows.admin_users = masterRow;

    for (let i = 1; i <= 5; i++) {
      const res = await loginAdmin('master@x.com', 'wrong');
      expect(res.admin).toBeNull();
      expect(res.error).toBe('Email ou senha incorretos');
    }

    // Apos 5 falhas: contador em 5 e conta bloqueada no futuro.
    expect(masterRow.failed_login_attempts).toBe(5);
    expect(masterRow.account_locked_until).toBeTruthy();
    expect(new Date(masterRow.account_locked_until as string).getTime()).toBeGreaterThan(
      Date.now(),
    );

    const updatesBefore = cfg.updates.length;

    // 6a tentativa: cai no ramo de conta bloqueada — erro generico, SEM novo update
    // (contador nao incrementa) e SEM revelar o motivo do bloqueio.
    const sixth = await loginAdmin('master@x.com', 'wrong');
    expect(sixth.admin).toBeNull();
    expect(sixth.error).toBe('Email ou senha incorretos');
    expect(masterRow.failed_login_attempts).toBe(5);
    expect(cfg.updates.length).toBe(updatesBefore);
  });

  it('login bem-sucedido reseta o contador de falhas', async () => {
    const masterRow = {
      id: 'm2',
      email: 'master2@x.com',
      role: 'master_admin',
      // Hash legacy de 'right' para autenticar com sucesso via caminho SHA-256.
      password_hash: await sha256Hex('right'),
      failed_login_attempts: 3,
      account_locked_until: null as string | null,
    };
    cfg.liveRows.admin_users = masterRow;

    const res = await loginAdmin('master2@x.com', 'right');
    expect(res.error).toBeNull();
    expect(res.admin?.id).toBe('m2');
    expect(masterRow.failed_login_attempts).toBe(0);
    expect(masterRow.account_locked_until).toBeNull();
  });
});

// ========================================================================== //
// ALTO-001 — loginAdmin lockout (company_admin / users_v2)
// ========================================================================== //
describe('ALTO-001 loginAdmin lockout — company_admin (users_v2)', () => {
  it('5 falhas bloqueiam a conta; 6a tentativa = conta bloqueada (erro generico)', async () => {
    // admin_users vazio (master nao encontrado) -> cai no caminho company admin.
    cfg.reads.admin_users = Array.from({ length: 8 }, () => ({ data: null }));
    const companyRow = {
      id: 'c1',
      email: 'cadmin@x.com',
      first_name: 'C',
      last_name: 'Admin',
      company_id: null,
      role: 'admin_company',
      status: 'active',
      password_hash: INVALID_HASH,
      failed_login_attempts: 0,
      account_locked_until: null as string | null,
    };
    cfg.liveRows.users_v2 = companyRow;

    for (let i = 1; i <= 5; i++) {
      const res = await loginAdmin('cadmin@x.com', 'wrong');
      expect(res.admin).toBeNull();
      expect(res.error).toBe('Email ou senha incorretos');
    }

    expect(companyRow.failed_login_attempts).toBe(5);
    expect(companyRow.account_locked_until).toBeTruthy();

    const updatesBefore = cfg.updates.length;
    const sixth = await loginAdmin('cadmin@x.com', 'wrong');
    expect(sixth.error).toBe('Email ou senha incorretos');
    expect(companyRow.failed_login_attempts).toBe(5);
    expect(cfg.updates.length).toBe(updatesBefore);
  });
});

// ========================================================================== //
// MEDIO-003 — createUser anti-enumeracao (mensagem nao-diferencial)
// ========================================================================== //
describe('MEDIO-003 createUser — mensagem generica nao-diferencial', () => {
  function signup(overrides: Partial<SignupData> = {}): SignupData {
    return {
      firstName: 'Joao',
      lastName: 'Silva',
      cpf: VALID_CPF,
      phone: '11999999999',
      email: 'joao@x.com',
      birthDate: '01/01/1990',
      password: 'Sup3rSenh@!2026',
      termsAccepted: true,
      ...overrides,
    };
  }

  it('email duplicado e CPF duplicado retornam EXATAMENTE a mesma mensagem', async () => {
    // Caso A: email duplicado (1a leitura users_v2 = existe).
    cfg.reads.users_v2 = [{ data: { id: 'existing-email' } }];
    const emailDup = await createUser(signup());

    // Caso B: CPF duplicado (email livre, 2a leitura users_v2 = existe).
    resetConfig();
    cfg.reads.users_v2 = [{ data: null }, { data: { id: 'existing-cpf' } }];
    const cpfDup = await createUser(signup());

    expect(emailDup.user).toBeNull();
    expect(cpfDup.user).toBeNull();
    // Nao-diferencial: as duas mensagens sao identicas e iguais a copy canonica.
    expect(emailDup.error).toBe(DUPLICATE_SIGNUP_ERROR);
    expect(cpfDup.error).toBe(DUPLICATE_SIGNUP_ERROR);
    expect(emailDup.error).toBe(cpfDup.error);
    // E nao revela qual campo colidiu.
    expect(emailDup.error).not.toMatch(/email/i);
    expect(emailDup.error).not.toMatch(/cpf/i);
  });
});

// Helper: SHA-256 hex (mesmo formato do hashPasswordLegacy de lib/auth).
async function sha256Hex(input: string): Promise<string> {
  const data = new TextEncoder().encode(input);
  const buf = await crypto.subtle.digest('SHA-256', data);
  return Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}
