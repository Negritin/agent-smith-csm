/**
 * S6 — Testes de ROTA (vitest) dos critérios de aceite que exigem teste de
 * ENDPOINT (não só de função pura/RPC). Cobre:
 *
 *  - messages [id] e legada: type inválido -> 400 SEM RPC/insert; text+image_url
 *    -> 200 chamando callTransition com action='record_human_message'; nenhuma
 *    rota nova faz UPDATE direto de conversations.status (D1/§8.1);
 *  - reopen: chama a RPC com p_action='reopen' e p_actor_type='human';
 *  - master_admin: actor_user_id/sender_user_id/created_by viram NULL (sem 23503),
 *    e company_id é obrigatório como query;
 *  - sla-policy PUT: unicidade (2 PUTs => UPDATE, não 2ª linha ativa); 1º PUT sem
 *    política => INSERT; master_admin sem company_id => 400; created_by null p/ master;
 *  - handoff-recipients: sync recipient<->blocklist (cria/reativa/desativa quando
 *    único / mantém quando há outro ativo; escopo agent_id null vs not-null);
 *  - attendance-settings PATCH: preserva csv_analytics/chave extra no tools_config
 *    e espelha human_handoff/end_attendance; GET retorna defaults sem registro.
 *
 * Runner ATIVO: `npm test` (vitest). Veja vitest.config.ts.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createFakeSupabase, type FakeSupabase } from './helpers/fake-supabase';
import type { AdminSessionData } from '@/lib/iron-session';

// --- Estado de mocks compartilhado entre os testes ---------------------------
let fake: FakeSupabase;
let session: AdminSessionData;

// Mock do client admin: todas as rotas usam getSupabaseAdmin() em import-time e
// guardam a referência em `const supabaseAdmin`. Como o módulo é cacheado entre os
// testes, devolvemos um PROXY ESTÁVEL que delega dinamicamente ao `fake.client`
// corrente (reatribuído em cada teste) em vez de capturar uma instância.
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

// Auth: devolve a `session` corrente.
vi.mock('@/lib/auth-actions', () => ({
  requireAdminSession: async () => ({ session }),
  auditCrossTenantAttempt: async () => undefined,
}));

// admin-proxy / internal-jwt: usados por deliverHumanMessage e fetchSlaInputs.
vi.mock('@/lib/admin-proxy', () => ({
  getAdminApiKeyOrResponse: () => ({ adminApiKey: 'test-key' }),
}));
vi.mock('@/lib/internal-jwt', () => ({
  createInternalAuthHeadersForAdminSession: () => ({ Authorization: 'Bearer test' }),
}));

const COMPANY_ID = '11111111-1111-1111-1111-111111111111';
const CONV_ID = '22222222-2222-2222-2222-222222222222';
const AGENT_ID = '33333333-3333-3333-3333-333333333333';
const COMPANY_ADMIN_ID = '44444444-4444-4444-4444-444444444444';
const MASTER_ADMIN_ID = '55555555-5555-5555-5555-555555555555';

function companyAdminSession(): AdminSessionData {
  return {
    adminId: COMPANY_ADMIN_ID,
    email: 'admin@co.com',
    name: 'Admin',
    role: 'company_admin',
    companyId: COMPANY_ID,
    expiresAt: new Date(Date.now() + 3600_000).toISOString(),
  };
}
function masterAdminSession(): AdminSessionData {
  return {
    adminId: MASTER_ADMIN_ID,
    email: 'master@co.com',
    name: 'Master',
    role: 'master_admin',
    companyId: null,
    expiresAt: new Date(Date.now() + 3600_000).toISOString(),
  };
}

function convRow(extra: Record<string, unknown> = {}) {
  return {
    id: CONV_ID,
    company_id: COMPANY_ID,
    agent_id: AGENT_ID,
    session_id: null,
    status: 'HUMAN_REQUESTED',
    channel: 'web',
    user_phone: null,
    ...extra,
  };
}

function req(url: string, body?: unknown): Request {
  return new Request(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

beforeEach(() => {
  session = companyAdminSession();
  // fetch não deve ser realmente chamado nos casos web (channel != whatsapp).
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify({}), { status: 200 })),
  );
});

// ========================================================================== //
// messages [id]/route.ts e legada messages/route.ts
// ========================================================================== //
describe('POST [id]/messages (§7.1/§9.1) — type, RPC, sem update direto', () => {
  it('type inválido (image) -> 400 SEM chamar RPC nem insert', async () => {
    fake = createFakeSupabase();
    const { POST } = await import('@/app/api/admin/conversations/[id]/messages/route');
    const res = await POST(
      req(`http://t/api/admin/conversations/${CONV_ID}/messages`, {
        content: 'oi',
        type: 'image',
      }) as any,
      { params: Promise.resolve({ id: CONV_ID }) },
    );
    expect(res.status).toBe(400);
    expect(fake.rpcCalls).toHaveLength(0);
    expect(fake.writes.filter((w) => w.table === 'messages')).toHaveLength(0);
  });

  it('text + image_url -> 200, RPC record_human_message, sem update direto de status', async () => {
    fake = createFakeSupabase({
      tables: {
        conversations: { selectResults: [{ data: convRow() }], writeResult: { data: [{}] } },
        messages: { writeResult: { data: { id: 'msg-1' } } },
      },
      rpcResults: {
        rpc_attendance_transition: {
          data: [
            {
              status: 'PENDING_CUSTOMER',
              previous_status: 'HUMAN_REQUESTED',
              conversation_id: CONV_ID,
              attendance_session_id: 'sess-1',
              attendance_sla_id: null,
              event_id: 'ev-1',
            },
          ],
        },
      },
    });
    const { POST } = await import('@/app/api/admin/conversations/[id]/messages/route');
    const res = await POST(
      req(`http://t/api/admin/conversations/${CONV_ID}/messages`, {
        content: 'olha a imagem',
        image_url: 'https://x/y.png',
        type: 'text',
      }) as any,
      { params: Promise.resolve({ id: CONV_ID }) },
    );
    expect(res.status).toBe(200);
    expect(fake.rpcCalls).toHaveLength(1);
    expect(fake.rpcCalls[0].name).toBe('rpc_attendance_transition');
    expect(fake.rpcCalls[0].args.p_action).toBe('record_human_message');
    expect(fake.rpcCalls[0].args.p_actor_type).toBe('human');
    // D1/§8.1: nenhuma escrita direta de conversations.status.
    expect(fake.conversationsStatusUpdates).toHaveLength(0);
  });

  it('master_admin: sender_user_id e p_actor_user_id viram NULL (sem FK 23503)', async () => {
    session = masterAdminSession();
    fake = createFakeSupabase({
      tables: {
        conversations: { selectResults: [{ data: convRow() }], writeResult: { data: [{}] } },
        messages: { writeResult: { data: { id: 'msg-1' } } },
      },
      rpcResults: {
        rpc_attendance_transition: {
          data: [
            {
              status: 'PENDING_CUSTOMER',
              previous_status: 'HUMAN_REQUESTED',
              conversation_id: CONV_ID,
              attendance_session_id: 'sess-1',
              attendance_sla_id: null,
              event_id: 'ev-1',
            },
          ],
        },
      },
    });
    const { POST } = await import('@/app/api/admin/conversations/[id]/messages/route');
    const res = await POST(
      req(`http://t/api/admin/conversations/${CONV_ID}/messages?company_id=${COMPANY_ID}`, {
        content: 'oi',
      }) as any,
      { params: Promise.resolve({ id: CONV_ID }) },
    );
    expect(res.status).toBe(200);
    expect(fake.rpcCalls[0].args.p_actor_user_id).toBeNull();
    const insert = fake.writes.find((w) => w.table === 'messages' && w.op === 'insert');
    expect((insert!.values as Record<string, unknown>).sender_user_id).toBeNull();
  });

  it('legada messages: delega a record_human_message (sem update direto)', async () => {
    fake = createFakeSupabase({
      tables: {
        conversations: { selectResults: [{ data: convRow() }], writeResult: { data: [{}] } },
        messages: { writeResult: { data: { id: 'msg-1' } } },
      },
      rpcResults: {
        rpc_attendance_transition: {
          data: [
            {
              status: 'PENDING_CUSTOMER',
              previous_status: 'HUMAN_REQUESTED',
              conversation_id: CONV_ID,
              attendance_session_id: 'sess-1',
              attendance_sla_id: null,
              event_id: 'ev-1',
            },
          ],
        },
      },
    });
    const { POST } = await import('@/app/api/admin/conversations/messages/route');
    const res = await POST(
      req('http://t/api/admin/conversations/messages', {
        conversation_id: CONV_ID,
        content: 'oi',
      }) as any,
    );
    expect(res.status).toBe(200);
    expect(fake.rpcCalls[0].args.p_action).toBe('record_human_message');
    expect(fake.conversationsStatusUpdates).toHaveLength(0);
  });
});

// ========================================================================== //
// reopen
// ========================================================================== //
describe('POST [id]/reopen (§6.2/§9.1)', () => {
  it('chama a RPC com p_action=reopen e p_actor_type=human (reopened_by_admin)', async () => {
    fake = createFakeSupabase({
      tables: {
        conversations: { selectResults: [{ data: convRow({ status: 'CLOSED' }) }] },
      },
      rpcResults: {
        rpc_attendance_transition: {
          data: [
            {
              status: 'open',
              previous_status: 'CLOSED',
              conversation_id: CONV_ID,
              attendance_session_id: 'sess-2',
              attendance_sla_id: null,
              event_id: 'ev-2',
            },
          ],
        },
      },
    });
    const { POST } = await import('@/app/api/admin/conversations/[id]/reopen/route');
    const res = await POST(req(`http://t/api/admin/conversations/${CONV_ID}/reopen`) as any, {
      params: Promise.resolve({ id: CONV_ID }),
    });
    expect(res.status).toBe(200);
    expect(fake.rpcCalls[0].args.p_action).toBe('reopen');
    expect(fake.rpcCalls[0].args.p_actor_type).toBe('human');
    expect(fake.conversationsStatusUpdates).toHaveLength(0);
  });
});

// ========================================================================== //
// claim / handoff: master_admin requer company_id; sem update direto
// ========================================================================== //
describe('POST [id]/claim — contrato master_admin (§9.1)', () => {
  it('master_admin sem company_id -> 400 (sem RPC)', async () => {
    session = masterAdminSession();
    fake = createFakeSupabase();
    const { POST } = await import('@/app/api/admin/conversations/[id]/claim/route');
    const res = await POST(req(`http://t/api/admin/conversations/${CONV_ID}/claim`) as any, {
      params: Promise.resolve({ id: CONV_ID }),
    });
    expect(res.status).toBe(400);
    expect(fake.rpcCalls).toHaveLength(0);
  });

  it('company_admin: p_actor_user_id = adminId; sem update direto de status', async () => {
    fake = createFakeSupabase({
      tables: { conversations: { selectResults: [{ data: convRow() }] } },
      rpcResults: {
        rpc_attendance_transition: {
          data: [
            {
              status: 'HUMAN_ACTIVE',
              previous_status: 'HUMAN_REQUESTED',
              conversation_id: CONV_ID,
              attendance_session_id: 'sess-3',
              attendance_sla_id: null,
              event_id: 'ev-3',
            },
          ],
        },
      },
    });
    const { POST } = await import('@/app/api/admin/conversations/[id]/claim/route');
    const res = await POST(req(`http://t/api/admin/conversations/${CONV_ID}/claim`) as any, {
      params: Promise.resolve({ id: CONV_ID }),
    });
    expect(res.status).toBe(200);
    expect(fake.rpcCalls[0].args.p_action).toBe('claim');
    expect(fake.rpcCalls[0].args.p_actor_user_id).toBe(COMPANY_ADMIN_ID);
    expect(fake.conversationsStatusUpdates).toHaveLength(0);
  });
});

// ========================================================================== //
// sla-policy PUT — unicidade + master_admin
// ========================================================================== //
describe('PUT /company/sla-policy (§9.2)', () => {
  it('com política ativa existente: faz UPDATE (não cria 2ª ativa)', async () => {
    fake = createFakeSupabase({
      tables: {
        sla_policies: {
          // 1ª leitura: existing (id). 2ª: a updated retornada.
          selectResults: [{ data: { id: 'pol-1' } }, { data: { id: 'pol-1', is_active: true } }],
          writeResult: { data: { id: 'pol-1', is_active: true }, error: null },
        },
      },
    });
    const { PUT } = await import('@/app/api/admin/company/sla-policy/route');
    const res = await PUT(
      new Request('http://t/api/admin/company/sla-policy', {
        method: 'PUT',
        body: JSON.stringify({ default_sla_level: 'high' }),
      }) as any,
    );
    expect(res.status).toBe(200);
    const updates = fake.writes.filter((w) => w.table === 'sla_policies' && w.op === 'update');
    const inserts = fake.writes.filter((w) => w.table === 'sla_policies' && w.op === 'insert');
    expect(updates).toHaveLength(1);
    expect(inserts).toHaveLength(0);
  });

  it('sem política prévia: faz INSERT', async () => {
    fake = createFakeSupabase({
      tables: {
        sla_policies: {
          selectResults: [{ data: null }, { data: { id: 'pol-new', is_active: true } }],
          writeResult: { data: { id: 'pol-new', is_active: true } },
        },
      },
    });
    const { PUT } = await import('@/app/api/admin/company/sla-policy/route');
    const res = await PUT(
      new Request('http://t/api/admin/company/sla-policy', {
        method: 'PUT',
        body: JSON.stringify({ name: 'P' }),
      }) as any,
    );
    expect(res.status).toBe(200);
    const inserts = fake.writes.filter((w) => w.table === 'sla_policies' && w.op === 'insert');
    expect(inserts).toHaveLength(1);
    expect((inserts[0].values as Record<string, unknown>).is_active).toBe(true);
  });

  it('master_admin sem company_id -> 400', async () => {
    session = masterAdminSession();
    fake = createFakeSupabase();
    const { PUT } = await import('@/app/api/admin/company/sla-policy/route');
    const res = await PUT(
      new Request('http://t/api/admin/company/sla-policy', {
        method: 'PUT',
        body: JSON.stringify({ name: 'P' }),
      }) as any,
    );
    expect(res.status).toBe(400);
  });

  it('master_admin: created_by/updated_by viram NULL (sem FK 23503)', async () => {
    session = masterAdminSession();
    fake = createFakeSupabase({
      tables: {
        sla_policies: {
          selectResults: [{ data: null }, { data: { id: 'pol-new' } }],
          writeResult: { data: { id: 'pol-new' } },
        },
      },
    });
    const { PUT } = await import('@/app/api/admin/company/sla-policy/route');
    const res = await PUT(
      new Request(`http://t/api/admin/company/sla-policy?company_id=${COMPANY_ID}`, {
        method: 'PUT',
        body: JSON.stringify({ name: 'P' }),
      }) as any,
    );
    expect(res.status).toBe(200);
    const insert = fake.writes.find((w) => w.table === 'sla_policies' && w.op === 'insert');
    expect((insert!.values as Record<string, unknown>).created_by).toBeNull();
    expect((insert!.values as Record<string, unknown>).updated_by).toBeNull();
  });
});

// ========================================================================== //
// company/attendance-settings — auto-close company-level (§16)
// ========================================================================== //
describe('GET/PUT /company/attendance-settings (§16)', () => {
  it('GET sem registro -> defaults company-level (auto-close OFF)', async () => {
    fake = createFakeSupabase({
      tables: {
        company_attendance_settings: { selectResults: [{ data: null }] },
      },
    });
    const { GET } = await import('@/app/api/admin/company/attendance-settings/route');
    const res = await GET(new Request('http://t/api/admin/company/attendance-settings') as any);
    expect(res.status).toBe(200);
    const json = (await res.json()) as { settings: Record<string, unknown> };
    expect(json.settings.auto_close_enabled).toBe(false);
    expect(json.settings.auto_close_after_minutes).toBe(240);
    expect(json.settings.auto_close_scope).toBe('all_attendance');
    expect(json.settings.company_id).toBe(COMPANY_ID);
  });

  it('PUT faz upsert por company_id', async () => {
    fake = createFakeSupabase({
      tables: {
        company_attendance_settings: {
          // 1ª leitura: existing (null). upsert retorna saved.
          selectResults: [{ data: null }],
          writeResult: { data: { company_id: COMPANY_ID, auto_close_enabled: true } },
        },
      },
    });
    const { PUT } = await import('@/app/api/admin/company/attendance-settings/route');
    const res = await PUT(
      new Request('http://t/api/admin/company/attendance-settings', {
        method: 'PUT',
        body: JSON.stringify({ auto_close_enabled: true, auto_close_after_minutes: 120 }),
      }) as any,
    );
    expect(res.status).toBe(200);
    const upserts = fake.writes.filter(
      (w) => w.table === 'company_attendance_settings' && w.op === 'upsert',
    );
    expect(upserts).toHaveLength(1);
    const values = upserts[0].values as Record<string, unknown>;
    expect(values.company_id).toBe(COMPANY_ID);
    expect(values.auto_close_enabled).toBe(true);
    expect(values.auto_close_after_minutes).toBe(120);
  });

  it('PUT após_minutes < 5 -> 400 SEM upsert', async () => {
    fake = createFakeSupabase({
      tables: { company_attendance_settings: { selectResults: [{ data: null }] } },
    });
    const { PUT } = await import('@/app/api/admin/company/attendance-settings/route');
    const res = await PUT(
      new Request('http://t/api/admin/company/attendance-settings', {
        method: 'PUT',
        body: JSON.stringify({ auto_close_after_minutes: 3 }),
      }) as any,
    );
    expect(res.status).toBe(400);
    expect(
      fake.writes.filter((w) => w.table === 'company_attendance_settings'),
    ).toHaveLength(0);
  });

  it('PUT mensagem habilitada mas vazia -> 400 SEM upsert', async () => {
    fake = createFakeSupabase({
      tables: { company_attendance_settings: { selectResults: [{ data: null }] } },
    });
    const { PUT } = await import('@/app/api/admin/company/attendance-settings/route');
    const res = await PUT(
      new Request('http://t/api/admin/company/attendance-settings', {
        method: 'PUT',
        body: JSON.stringify({ auto_close_message_enabled: true, auto_close_message: '   ' }),
      }) as any,
    );
    expect(res.status).toBe(400);
    expect(
      fake.writes.filter((w) => w.table === 'company_attendance_settings'),
    ).toHaveLength(0);
  });

  it('master_admin sem company_id -> 400', async () => {
    session = masterAdminSession();
    fake = createFakeSupabase();
    const { GET } = await import('@/app/api/admin/company/attendance-settings/route');
    const res = await GET(new Request('http://t/api/admin/company/attendance-settings') as any);
    expect(res.status).toBe(400);
  });
});

// ========================================================================== //
// handoff-recipients — sincronização recipient <-> blocklist (§9.4/§8.4)
// ========================================================================== //
describe('POST /handoff-recipients (§9.4) — sync blocklist', () => {
  it('(a) cria recipient WhatsApp + cria blocklist derivada', async () => {
    fake = createFakeSupabase({
      tables: {
        handoff_notification_recipients: {
          selectResults: [{ data: null }], // não existe ainda
          writeResult: { data: { id: 'rec-1' } },
        },
        internal_whatsapp_blocklist: {
          selectResults: [{ data: null }], // não existe ainda -> insert
        },
      },
    });
    const { POST } = await import('@/app/api/admin/handoff-recipients/route');
    const res = await POST(
      new Request('http://t/api/admin/handoff-recipients', {
        method: 'POST',
        body: JSON.stringify({ channel: 'whatsapp', recipient_value: '11987654321' }),
      }) as any,
    );
    expect(res.status).toBe(200);
    expect(
      fake.writes.filter((w) => w.table === 'handoff_notification_recipients' && w.op === 'insert'),
    ).toHaveLength(1);
    const blockInserts = fake.writes.filter(
      (w) => w.table === 'internal_whatsapp_blocklist' && w.op === 'insert',
    );
    expect(blockInserts).toHaveLength(1);
    expect((blockInserts[0].values as Record<string, unknown>).phone_normalized).toBe(
      '5511987654321',
    );
  });

  it('(b) POST repetido reativa recipient + reativa blocklist (sem violar unicidade)', async () => {
    fake = createFakeSupabase({
      tables: {
        handoff_notification_recipients: {
          selectResults: [{ data: { id: 'rec-1', enabled: false } }], // já existe -> update
          writeResult: { data: { id: 'rec-1' } },
        },
        internal_whatsapp_blocklist: {
          selectResults: [{ data: { id: 'blk-1' } }], // já existe -> update active=true
        },
      },
    });
    const { POST } = await import('@/app/api/admin/handoff-recipients/route');
    const res = await POST(
      new Request('http://t/api/admin/handoff-recipients', {
        method: 'POST',
        body: JSON.stringify({ channel: 'whatsapp', recipient_value: '11987654321' }),
      }) as any,
    );
    expect(res.status).toBe(200);
    const recUpdates = fake.writes.filter(
      (w) => w.table === 'handoff_notification_recipients' && w.op === 'update',
    );
    expect(recUpdates).toHaveLength(1);
    expect((recUpdates[0].values as Record<string, unknown>).enabled).toBe(true);
    const blockUpdates = fake.writes.filter(
      (w) => w.table === 'internal_whatsapp_blocklist' && w.op === 'update',
    );
    expect(blockUpdates).toHaveLength(1);
    expect((blockUpdates[0].values as Record<string, unknown>).active).toBe(true);
    expect(
      fake.writes.filter((w) => w.table === 'internal_whatsapp_blocklist' && w.op === 'insert'),
    ).toHaveLength(0);
  });

  it('(c/d/e) master_admin: created_by NULL e blocklist escopada por agent_id', async () => {
    session = masterAdminSession();
    fake = createFakeSupabase({
      tables: {
        handoff_notification_recipients: {
          selectResults: [{ data: null }],
          writeResult: { data: { id: 'rec-2' } },
        },
        internal_whatsapp_blocklist: { selectResults: [{ data: null }] },
      },
    });
    const { POST } = await import('@/app/api/admin/handoff-recipients/route');
    const res = await POST(
      new Request(`http://t/api/admin/handoff-recipients?company_id=${COMPANY_ID}`, {
        method: 'POST',
        body: JSON.stringify({
          channel: 'whatsapp',
          recipient_value: '11987654321',
          agent_id: AGENT_ID,
        }),
      }) as any,
    );
    expect(res.status).toBe(200);
    const insert = fake.writes.find(
      (w) => w.table === 'handoff_notification_recipients' && w.op === 'insert',
    );
    expect((insert!.values as Record<string, unknown>).created_by).toBeNull();
    const blockInsert = fake.writes.find(
      (w) => w.table === 'internal_whatsapp_blocklist' && w.op === 'insert',
    );
    expect((blockInsert!.values as Record<string, unknown>).agent_id).toBe(AGENT_ID);
  });
});

describe('DELETE /handoff-recipients/[id] (§9.4) — sync on disable', () => {
  it('desativa blocklist quando é o ÚNICO recipient ativo do número', async () => {
    fake = createFakeSupabase({
      tables: {
        handoff_notification_recipients: {
          selectResults: [
            // resolveRecipient
            {
              data: {
                id: 'rec-1',
                company_id: COMPANY_ID,
                agent_id: null,
                channel: 'whatsapp',
                recipient_normalized: '5511987654321',
              },
            },
            // syncBlocklistOnDisable: "others" => vazio (nenhum outro ativo)
            { data: [] },
          ],
        },
        internal_whatsapp_blocklist: {},
      },
    });
    const { DELETE } = await import('@/app/api/admin/handoff-recipients/[id]/route');
    const res = await DELETE(
      new Request('http://t/api/admin/handoff-recipients/rec-1', { method: 'DELETE' }) as any,
      { params: Promise.resolve({ id: 'rec-1' }) },
    );
    expect(res.status).toBe(200);
    const blockUpdates = fake.writes.filter(
      (w) => w.table === 'internal_whatsapp_blocklist' && w.op === 'update',
    );
    expect(blockUpdates).toHaveLength(1);
    expect((blockUpdates[0].values as Record<string, unknown>).active).toBe(false);
  });

  it('NÃO desativa blocklist quando há OUTRO recipient ativo com o mesmo número', async () => {
    fake = createFakeSupabase({
      tables: {
        handoff_notification_recipients: {
          selectResults: [
            {
              data: {
                id: 'rec-1',
                company_id: COMPANY_ID,
                agent_id: null,
                channel: 'whatsapp',
                recipient_normalized: '5511987654321',
              },
            },
            // "others" => existe outro ativo
            { data: [{ id: 'rec-2' }] },
          ],
        },
        internal_whatsapp_blocklist: {},
      },
    });
    const { DELETE } = await import('@/app/api/admin/handoff-recipients/[id]/route');
    const res = await DELETE(
      new Request('http://t/api/admin/handoff-recipients/rec-1', { method: 'DELETE' }) as any,
      { params: Promise.resolve({ id: 'rec-1' }) },
    );
    expect(res.status).toBe(200);
    const blockUpdates = fake.writes.filter(
      (w) => w.table === 'internal_whatsapp_blocklist' && w.op === 'update',
    );
    expect(blockUpdates).toHaveLength(0);
  });
});

// ========================================================================== //
// attendance-settings — preserva csv_analytics (CASO OBRIGATÓRIO §9.3)
// ========================================================================== //
describe('attendance-settings (§9.3) — rota PATCH/GET', () => {
  it('PATCH preserva csv_analytics e chave extra; espelha handoff/end_attendance', async () => {
    fake = createFakeSupabase({
      tables: {
        agents: {
          selectResults: [
            {
              data: {
                id: AGENT_ID,
                company_id: COMPANY_ID,
                tools_config: {
                  csv_analytics: { enabled: true },
                  foo: { bar: 1 },
                  human_handoff: { enabled: false },
                },
              },
            },
          ],
          writeResult: { data: [{}] },
        },
        agent_attendance_settings: {
          // PATCH lê existing (null), faz upsert, relê saved.
          selectResults: [{ data: null }, { data: { agent_id: AGENT_ID, handoff_enabled: true } }],
          writeResult: { data: [{}] },
        },
      },
    });
    const { PATCH } = await import('@/app/api/admin/agents/[agentId]/attendance-settings/route');
    const res = await PATCH(
      new Request(`http://t/api/admin/agents/${AGENT_ID}/attendance-settings`, {
        method: 'PATCH',
        body: JSON.stringify({ handoff_enabled: true, agent_can_close: true }),
      }) as any,
      { params: Promise.resolve({ agentId: AGENT_ID }) },
    );
    expect(res.status).toBe(200);
    const agentUpdate = fake.writes.find((w) => w.table === 'agents' && w.op === 'update');
    const toolsConfig = (agentUpdate!.values as Record<string, unknown>).tools_config as Record<
      string,
      unknown
    >;
    expect(toolsConfig.csv_analytics).toEqual({ enabled: true });
    expect(toolsConfig.foo).toEqual({ bar: 1 });
    expect(toolsConfig.human_handoff).toEqual({ enabled: true });
    expect(toolsConfig.end_attendance).toEqual({ enabled: true });
    // updated_at bumpado.
    expect((agentUpdate!.values as Record<string, unknown>).updated_at).toBeTruthy();
  });

  it('GET retorna defaults quando não há registro', async () => {
    fake = createFakeSupabase({
      tables: {
        agents: {
          selectResults: [{ data: { id: AGENT_ID, company_id: COMPANY_ID, tools_config: null } }],
        },
        agent_attendance_settings: { selectResults: [{ data: null }] },
      },
    });
    const { GET } = await import('@/app/api/admin/agents/[agentId]/attendance-settings/route');
    const res = await GET(
      new Request(`http://t/api/admin/agents/${AGENT_ID}/attendance-settings`) as any,
      { params: Promise.resolve({ agentId: AGENT_ID }) },
    );
    expect(res.status).toBe(200);
    const json = (await res.json()) as { settings: Record<string, unknown> };
    expect(json.settings.handoff_enabled).toBe(false);
    expect(json.settings.reopen_on_customer_reply).toBe(true);
    expect(json.settings.agent_can_close).toBe(false);
    // auto_close_* virou config da EMPRESA (company_attendance_settings, §16): a
    // rota do AGENTE não retorna mais esses campos nos defaults.
    expect(json.settings.auto_close_after_minutes).toBeUndefined();
    expect(json.settings.auto_close_enabled).toBeUndefined();
  });
});
