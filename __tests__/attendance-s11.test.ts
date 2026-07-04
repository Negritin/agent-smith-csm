/**
 * S11 — Teste estático da migration de saneamento do `anon` (SPRINTS S11, §17/§24).
 *
 * Runner: vitest em ambiente `node` (ver vitest.config.ts). Este teste NÃO toca o
 * banco — ele faz ASSERTS ESTÁTICOS sobre o SQL das migrations de S11 para garantir:
 *
 *  1. A migration de REVOKE faz exatamente: REVOKE ALL em conversations/messages
 *     FROM anon + DROP POLICY IF EXISTS da policy de realtime anônima.
 *  2. O ROLLBACK é SIMÉTRICO: re-GRANT ALL nas MESMAS tabelas TO anon + recria a
 *     EXATA policy removida (com DROP IF EXISTS antes — idempotente).
 *  3. NENHUMA das duas migrations toca o caminho do widget: não há REVOKE de
 *     get_widget_messages_scoped / get_widget_agent_public (RPCs SECURITY DEFINER
 *     concedidas a `anon` por GRANT EXECUTE — preservadas).
 *  4. Idempotência: DROP POLICY usa IF EXISTS; REVOKE/GRANT são idempotentes.
 *
 * A simetria é validada comparando o CONJUNTO de objetos (tabela/policy) afetados
 * pelo REVOKE com o conjunto re-concedido/recriado pelo ROLLBACK.
 */
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createFakeSupabase, type FakeSupabase } from './helpers/fake-supabase';
import type { AdminSessionData } from '@/lib/iron-session';

// --- Mocks para a cobertura de ROTA do entregável #4 (auditoria §17.3) -------
// (Os blocos estáticos de migration acima NÃO dependem destes mocks.)
let fake: FakeSupabase;
let session: AdminSessionData;
const auditCrossTenantSpy = vi.fn(async (..._args: unknown[]) => undefined);

// Proxy estável que delega ao `fake.client` corrente (reatribuído por teste),
// igual ao padrão de attendance-s6-routes.test.ts (módulo de rota cacheado).
const stableClientProxy = new Proxy(
  {},
  {
    get(_t, prop) {
      const target = fake.client as Record<string | symbol, unknown>;
      const value = target[prop];
      return typeof value === 'function' ? value.bind(target) : value;
    },
  },
);
vi.mock('@/lib/supabase-admin', () => ({
  getSupabaseAdmin: () => stableClientProxy,
}));
vi.mock('@/lib/auth-actions', () => ({
  requireAdminSession: async () => ({ session }),
  // Diferente do s6-routes (no-op): aqui é um SPY para assertar a auditoria.
  auditCrossTenantAttempt: (...args: unknown[]) => auditCrossTenantSpy(...args),
}));

const MIGRATIONS_DIR = join(
  __dirname,
  '..',
  'backend',
  'supabase',
  'migrations',
);

// O ROLLBACK vive FORA de migrations/ de propósito (passo manual de incidente):
// se estivesse em migrations/ o runner o aplicaria logo após o forward REVOKE e
// desfaria o saneamento no mesmo deploy. Ver header das duas migrations.
const ROLLBACKS_DIR = join(__dirname, '..', 'backend', 'supabase', 'rollbacks');

const REVOKE_SQL = readFileSync(
  join(MIGRATIONS_DIR, '20260624_revoke_anon_attendance.sql'),
  'utf8',
);
const ROLLBACK_SQL = readFileSync(
  join(ROLLBACKS_DIR, '20260624_revoke_anon_attendance_ROLLBACK.sql'),
  'utf8',
);

/** Remove comentários de linha `-- ...` para inspecionar apenas o SQL executável. */
function stripComments(sql: string): string {
  return sql
    .split('\n')
    .filter((line) => !line.trimStart().startsWith('--'))
    .join('\n');
}

const REVOKE_CODE = stripComments(REVOKE_SQL);
const ROLLBACK_CODE = stripComments(ROLLBACK_SQL);

const POLICY_NAME = 'Allow realtime subscriptions on messages';
const REVOKED_TABLES = ['public.conversations', 'public.messages'] as const;

describe('S11 — ROLLBACK NÃO pode viver em migrations/ (anti-regressão)', () => {
  // BLOCKER fechado: se o rollback voltar para migrations/, o runner o aplicaria
  // logo após o forward REVOKE (ordem lexicográfica) e desfaria o saneamento no
  // mesmo deploy. Este teste FALHA se alguém recolocar o arquivo lá.
  it('o arquivo de ROLLBACK não está em backend/supabase/migrations/', () => {
    const strayInMigrations = join(
      MIGRATIONS_DIR,
      '20260624_revoke_anon_attendance_ROLLBACK.sql',
    );
    expect(existsSync(strayInMigrations)).toBe(false);
  });

  it('o forward REVOKE permanece em migrations/ e o ROLLBACK em rollbacks/', () => {
    expect(
      existsSync(join(MIGRATIONS_DIR, '20260624_revoke_anon_attendance.sql')),
    ).toBe(true);
    expect(
      existsSync(
        join(ROLLBACKS_DIR, '20260624_revoke_anon_attendance_ROLLBACK.sql'),
      ),
    ).toBe(true);
  });
});

describe('S11 migration — REVOKE anon (saneamento)', () => {
  it('revoga ALL de anon em conversations e messages', () => {
    for (const table of REVOKED_TABLES) {
      const re = new RegExp(`REVOKE\\s+ALL\\s+ON\\s+${table.replace('.', '\\.')}\\s+FROM\\s+anon`, 'i');
      expect(REVOKE_CODE, `REVOKE ausente para ${table}`).toMatch(re);
    }
  });

  it('remove a policy de realtime anônima com DROP POLICY IF EXISTS (idempotente)', () => {
    const re = new RegExp(
      `DROP\\s+POLICY\\s+IF\\s+EXISTS\\s+"${POLICY_NAME}"\\s+ON\\s+public\\.messages`,
      'i',
    );
    expect(REVOKE_CODE).toMatch(re);
  });

  it('NÃO concede nada a anon (não há GRANT ... TO anon no REVOKE)', () => {
    expect(REVOKE_CODE).not.toMatch(/GRANT[\s\S]*?TO\s+anon/i);
  });

  it('NÃO toca o caminho do widget (sem REVOKE das RPCs escopadas)', () => {
    expect(REVOKE_CODE).not.toMatch(/get_widget_messages_scoped/i);
    expect(REVOKE_CODE).not.toMatch(/get_widget_agent_public/i);
  });
});

describe('S11 migration — ROLLBACK (reversa simétrica)', () => {
  it('re-concede ALL a anon nas MESMAS tabelas revogadas', () => {
    for (const table of REVOKED_TABLES) {
      const re = new RegExp(`GRANT\\s+ALL\\s+ON\\s+${table.replace('.', '\\.')}\\s+TO\\s+anon`, 'i');
      expect(ROLLBACK_CODE, `GRANT ausente para ${table}`).toMatch(re);
    }
  });

  it('recria a EXATA policy removida (TO anon USING(true)), com DROP IF EXISTS antes', () => {
    expect(ROLLBACK_CODE).toMatch(
      new RegExp(`DROP\\s+POLICY\\s+IF\\s+EXISTS\\s+"${POLICY_NAME}"\\s+ON\\s+public\\.messages`, 'i'),
    );
    const createRe = new RegExp(
      `CREATE\\s+POLICY\\s+"${POLICY_NAME}"[\\s\\S]*?ON\\s+public\\.messages[\\s\\S]*?FOR\\s+SELECT[\\s\\S]*?TO\\s+anon[\\s\\S]*?USING\\s*\\(\\s*true\\s*\\)`,
      'i',
    );
    expect(ROLLBACK_CODE).toMatch(createRe);
  });

  it('NÃO revoga nada (rollback só re-concede/recria)', () => {
    expect(ROLLBACK_CODE).not.toMatch(/REVOKE\b/i);
  });

  it('NÃO toca o caminho do widget', () => {
    expect(ROLLBACK_CODE).not.toMatch(/get_widget_messages_scoped/i);
    expect(ROLLBACK_CODE).not.toMatch(/get_widget_agent_public/i);
  });
});

describe('S11 — simetria REVOKE <-> ROLLBACK', () => {
  it('o conjunto de tabelas revogadas == conjunto re-concedido', () => {
    const revoked = REVOKED_TABLES.filter((t) =>
      new RegExp(`REVOKE\\s+ALL\\s+ON\\s+${t.replace('.', '\\.')}\\s+FROM\\s+anon`, 'i').test(REVOKE_CODE),
    );
    const granted = REVOKED_TABLES.filter((t) =>
      new RegExp(`GRANT\\s+ALL\\s+ON\\s+${t.replace('.', '\\.')}\\s+TO\\s+anon`, 'i').test(ROLLBACK_CODE),
    );
    expect(new Set(granted)).toEqual(new Set(revoked));
    expect(revoked).toHaveLength(REVOKED_TABLES.length);
  });

  it('a policy dropada no REVOKE é a MESMA recriada no ROLLBACK', () => {
    const dropRe = new RegExp(
      `DROP\\s+POLICY\\s+IF\\s+EXISTS\\s+"${POLICY_NAME}"\\s+ON\\s+public\\.messages`,
      'i',
    );
    expect(REVOKE_CODE).toMatch(dropRe);
    expect(ROLLBACK_CODE).toMatch(
      new RegExp(`CREATE\\s+POLICY\\s+"${POLICY_NAME}"`, 'i'),
    );
  });
});

// ========================================================================== //
// S11 entregável #4 (auditoria §17.3) — REGRESSÃO cross-tenant nas rotas de
// handoff-recipients/[id]. Garante que um company_admin do tenant A que mira um
// recipient do tenant B recebe 404 + auditCrossTenantAttempt (e NÃO escreve nada).
//
// Estes testes exercitam as ROTAS de verdade (importam PATCH/DELETE/POST), ao
// contrário dos blocos estáticos de migration acima. Cobrem a lacuna apontada na
// auditoria: o guard existe em handoff-recipients/[id]/route.ts:48-60 e
// [id]/test/route.ts:39-51 mas estava sem teste de regressão.
// ========================================================================== //
const TENANT_A = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
const TENANT_B = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
const ADMIN_A_ID = 'a0000000-0000-0000-0000-000000000001';
const RECIPIENT_ID = 'rec-cross-tenant';

function companyAdminSessionTenantA(): AdminSessionData {
  return {
    adminId: ADMIN_A_ID,
    email: 'admin-a@co.com',
    name: 'Admin A',
    role: 'company_admin',
    companyId: TENANT_A,
    expiresAt: new Date(Date.now() + 3600_000).toISOString(),
  };
}

/** Recipient pertencente ao tenant B (alvo cross-tenant). */
function recipientTenantB() {
  return {
    id: RECIPIENT_ID,
    company_id: TENANT_B,
    agent_id: null,
    channel: 'whatsapp',
    recipient_value: '11987654321',
    recipient_normalized: '5511987654321',
    enabled: true,
  };
}

describe('S11 #4 — rejeição cross-tenant em handoff-recipients/[id] (§17.3)', () => {
  beforeEach(() => {
    session = companyAdminSessionTenantA();
    auditCrossTenantSpy.mockClear();
    // Toda leitura de recipient devolve o registro do tenant B.
    fake = createFakeSupabase({
      tables: {
        handoff_notification_recipients: {
          selectResults: [
            { data: recipientTenantB() },
            { data: recipientTenantB() },
            { data: recipientTenantB() },
          ],
        },
      },
    });
  });

  it('PATCH: company_admin do tenant A -> 404 + auditCrossTenantAttempt, sem escrita', async () => {
    const { PATCH } = await import('@/app/api/admin/handoff-recipients/[id]/route');
    const res = await PATCH(
      new Request(`http://t/api/admin/handoff-recipients/${RECIPIENT_ID}`, {
        method: 'PATCH',
        body: JSON.stringify({ enabled: false }),
      }) as any,
      { params: Promise.resolve({ id: RECIPIENT_ID }) },
    );
    expect(res.status).toBe(404);
    expect(auditCrossTenantSpy).toHaveBeenCalledTimes(1);
    const auditArgs = auditCrossTenantSpy.mock.calls[0][0] as Record<string, unknown>;
    expect(auditArgs.actorCompanyId).toBe(TENANT_A);
    expect(auditArgs.targetCompanyId).toBe(TENANT_B);
    expect(auditArgs.action).toBe('modify_handoff_recipient');
    // Nenhuma escrita no recipient do outro tenant.
    expect(
      fake.writes.filter((w) => w.table === 'handoff_notification_recipients'),
    ).toHaveLength(0);
  });

  it('DELETE: company_admin do tenant A -> 404 + auditCrossTenantAttempt, sem escrita', async () => {
    const { DELETE } = await import('@/app/api/admin/handoff-recipients/[id]/route');
    const res = await DELETE(
      new Request(`http://t/api/admin/handoff-recipients/${RECIPIENT_ID}`, {
        method: 'DELETE',
      }) as any,
      { params: Promise.resolve({ id: RECIPIENT_ID }) },
    );
    expect(res.status).toBe(404);
    expect(auditCrossTenantSpy).toHaveBeenCalledTimes(1);
    const auditArgs = auditCrossTenantSpy.mock.calls[0][0] as Record<string, unknown>;
    expect(auditArgs.targetCompanyId).toBe(TENANT_B);
    expect(auditArgs.action).toBe('modify_handoff_recipient');
    expect(
      fake.writes.filter((w) => w.table === 'handoff_notification_recipients'),
    ).toHaveLength(0);
  });

  it('POST /test: company_admin do tenant A -> 404 + auditCrossTenantAttempt, sem enfileirar delivery', async () => {
    const { POST } = await import('@/app/api/admin/handoff-recipients/[id]/test/route');
    const res = await POST(
      new Request(`http://t/api/admin/handoff-recipients/${RECIPIENT_ID}/test`, {
        method: 'POST',
      }) as any,
      { params: Promise.resolve({ id: RECIPIENT_ID }) },
    );
    expect(res.status).toBe(404);
    expect(auditCrossTenantSpy).toHaveBeenCalledTimes(1);
    const auditArgs = auditCrossTenantSpy.mock.calls[0][0] as Record<string, unknown>;
    expect(auditArgs.targetCompanyId).toBe(TENANT_B);
    expect(auditArgs.action).toBe('test_handoff_recipient');
    // Não pode enfileirar notification_deliveries para o tenant alheio.
    expect(fake.writes.filter((w) => w.table === 'notification_deliveries')).toHaveLength(0);
  });
});
