/**
 * S7 — Testes de ROTA (vitest) da API de LEITURA + classificador de mensagens
 * legadas. Cobre:
 *
 *  - GET /[id]/details: retorna o contrato `ConversationDetails` (§9.1);
 *    conversa ANTIGA sem `attendance_sessions` => current_session=null e
 *    sla.health_status='none' (§22 itens 4-5); master_admin sem company_id => 400;
 *  - GET /conversations: paginação + chave `conversations` preservada; ordenação
 *    por prioridade §6.1 (HUMAN_REQUESTED > SLA breached/critical >
 *    HUMAN_ACTIVE/PENDING_CUSTOMER > demais por last_message_at); filtro canônico
 *    `status=human` agrupa os 3 estados humanos e `channel=widget` != `web`;
 *  - messageIsHuman: role='assistant'+sender_user_id (e author_type legado) =>
 *    humano (§22 item 3), role='user' => cliente.
 *
 * Runner ATIVO: `npm test` (vitest). Veja vitest.config.ts.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createFakeSupabase, type FakeSupabase } from './helpers/fake-supabase';
import type { AdminSessionData } from '@/lib/iron-session';
import { messageIsHuman } from '@/types/conversation-details';

let fake: FakeSupabase;
let session: AdminSessionData;

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
  auditCrossTenantAttempt: async () => undefined,
}));

const COMPANY_ID = '11111111-1111-1111-1111-111111111111';
const CONV_ID = '22222222-2222-2222-2222-222222222222';
const AGENT_ID = '33333333-3333-3333-3333-333333333333';

function companyAdminSession(): AdminSessionData {
  return {
    adminId: '44444444-4444-4444-4444-444444444444',
    email: 'admin@co.com',
    name: 'Admin',
    role: 'company_admin',
    companyId: COMPANY_ID,
    expiresAt: new Date(Date.now() + 3600_000).toISOString(),
  };
}
function masterAdminSession(): AdminSessionData {
  return {
    adminId: '55555555-5555-5555-5555-555555555555',
    email: 'master@co.com',
    name: 'Master',
    role: 'master_admin',
    companyId: null,
    expiresAt: new Date(Date.now() + 3600_000).toISOString(),
  };
}

function getReq(url: string): Request {
  return new Request(url, { method: 'GET' });
}

beforeEach(() => {
  session = companyAdminSession();
});

// ========================================================================== //
// messageIsHuman (§22 item 3) — classificador legado, fonte única
// ========================================================================== //
describe('messageIsHuman (§22 item 3)', () => {
  it('role=assistant + sender_user_id => humano (legado pré-backfill)', () => {
    expect(messageIsHuman({ role: 'assistant', sender_user_id: 'u-1' })).toBe(true);
  });
  it('author_type=human_operator => humano (backfill S1)', () => {
    expect(
      messageIsHuman({ role: 'assistant', author_type: 'human_operator', sender_user_id: null }),
    ).toBe(true);
  });
  it('role=assistant sem sender_user_id => IA', () => {
    expect(messageIsHuman({ role: 'assistant', sender_user_id: null })).toBe(false);
  });
  it('role=user => cliente (nunca humano-operador)', () => {
    expect(messageIsHuman({ role: 'user', sender_user_id: null })).toBe(false);
  });
});

// ========================================================================== //
// GET /[id]/details (§9.1)
// ========================================================================== //
describe('GET /[id]/details (§9.1)', () => {
  it('conversa ANTIGA sem sessão => current_session=null, sla.health_status=none', async () => {
    fake = createFakeSupabase({
      tables: {
        // 1) conversa (sem current_attendance_session_id, sem assigned_user_id)
        conversations: {
          selectResults: [
            {
              data: {
                id: CONV_ID,
                company_id: COMPANY_ID,
                agent_id: AGENT_ID,
                session_id: null,
                status: 'open',
                channel: 'web',
                user_id: null,
                user_name: 'Cliente',
                user_phone: null,
                user_avatar: null,
                agent_name: 'Smith',
                last_message_preview: 'oi',
                last_message_at: '2026-06-21T10:00:00Z',
                unread_count: 0,
                status_color: 'green',
                assigned_user_id: null,
                current_attendance_session_id: null,
                sla_priority: null,
                last_customer_message_at: null,
                last_human_message_at: null,
                last_ai_message_at: null,
                customer_waiting_since: null,
                agent_paused: false,
                created_at: '2026-06-21T09:00:00Z',
                agents: { id: AGENT_ID, name: 'Smith' },
              },
            },
          ],
        },
        // 2) eventos (vazio) 3) deliveries (vazio)
        conversation_events: { selectResults: [{ data: [] }] },
        notification_deliveries: { selectResults: [{ data: [] }] },
        conversation_inactivity_timers: { selectResults: [{ data: null }] },
      },
    });
    const { GET } = await import('@/app/api/admin/conversations/[id]/details/route');
    const res = await GET(getReq(`http://t/api/admin/conversations/${CONV_ID}/details`) as any, {
      params: Promise.resolve({ id: CONV_ID }),
    });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.conversation.id).toBe(CONV_ID);
    expect(body.current_session).toBeNull();
    expect(body.sla.health_status).toBe('none');
    expect(body.sla.level).toBeNull();
    expect(body.assignee).toBeNull();
    expect(Array.isArray(body.events)).toBe(true);
    expect(Array.isArray(body.notification_deliveries)).toBe(true);
    expect(body.active_timer).toBeNull();
  });

  it('conversa ENCERRADA (ponteiro null) recupera SLA settled + responsável da última sessão', async () => {
    // Regressão do "Sem SLA configurado"/"Não atribuído" pós-encerramento: o close zera
    // current_attendance_session_id, mas o details deve cair na última sessão da conversa
    // (com seu attendance_sla settled) e derivar o responsável de human_taken_by/closed_by.
    // Espelha o caso real de prod: human_taken_by null, closed_by = operador.
    const SESSION_ID = '66666666-6666-6666-6666-666666666666';
    const OPERATOR_ID = '77777777-7777-7777-7777-777777777777';
    fake = createFakeSupabase({
      tables: {
        conversations: {
          selectResults: [
            {
              data: {
                id: CONV_ID,
                company_id: COMPANY_ID,
                agent_id: AGENT_ID,
                session_id: null,
                status: 'CLOSED',
                channel: 'whatsapp',
                user_id: null,
                user_name: 'Cliente',
                user_phone: '5544999',
                user_avatar: null,
                agent_name: 'Smith',
                last_message_preview: 'ok',
                last_message_at: '2026-06-21T10:00:00Z',
                unread_count: 0,
                status_color: 'gray',
                assigned_user_id: null, // limpo no encerramento/return_to_ai
                current_attendance_session_id: null, // zerado no close
                sla_priority: null,
                last_customer_message_at: null,
                last_human_message_at: null,
                last_ai_message_at: null,
                customer_waiting_since: null,
                agent_paused: false,
                created_at: '2026-06-21T09:00:00Z',
                agents: { id: AGENT_ID, name: 'Smith' },
              },
            },
          ],
        },
        // Fallback: última sessão da conversa (encerrada). human_taken_by null + closed_by setado.
        attendance_sessions: {
          selectResults: [
            {
              data: {
                id: SESSION_ID,
                conversation_id: CONV_ID,
                company_id: COMPANY_ID,
                agent_id: AGENT_ID,
                user_id: null,
                channel: 'whatsapp',
                status: 'closed',
                started_at: '2026-06-21T09:30:00Z',
                human_requested_at: '2026-06-21T09:31:00Z',
                human_request_reason: null,
                human_taken_at: null,
                human_taken_by_user_id: null,
                first_human_response_at: null,
                returned_to_ai_at: null,
                resolved_at: null,
                closed_at: '2026-06-21T09:51:00Z',
                closed_by_type: 'human',
                closed_by_user_id: OPERATOR_ID,
                close_reason: null,
                close_summary: null,
                created_at: '2026-06-21T09:30:00Z',
                updated_at: '2026-06-21T09:51:00Z',
              },
            },
          ],
        },
        // SLA settled da sessão encerrada (estourado): deve aparecer no lugar de 'none'.
        attendance_sla: {
          selectResults: [
            {
              data: {
                health_status: 'breached',
                first_response_status: 'missed',
                resolution_status: 'missed',
                sla_level: 'normal',
                first_response_deadline: '2026-06-21T09:40:00Z',
                resolution_deadline: '2026-06-21T09:50:00Z',
                first_response_at: null,
                resolved_at: null,
              },
            },
          ],
        },
        conversation_events: { selectResults: [{ data: [] }] },
        notification_deliveries: { selectResults: [{ data: [] }] },
        conversation_inactivity_timers: { selectResults: [{ data: null }] },
        // Resolução do responsável (assignee) pelo closed_by_user_id da sessão.
        users_v2: {
          selectResults: [
            {
              data: {
                id: OPERATOR_ID,
                first_name: 'Op',
                last_name: 'Erador',
                email: 'op@co.com',
                avatar_url: null,
              },
            },
          ],
        },
      },
    });
    const { GET } = await import('@/app/api/admin/conversations/[id]/details/route');
    const res = await GET(getReq(`http://t/api/admin/conversations/${CONV_ID}/details`) as any, {
      params: Promise.resolve({ id: CONV_ID }),
    });
    expect(res.status).toBe(200);
    const body = await res.json();
    // SLA settled recuperado (não 'none').
    expect(body.sla.health_status).toBe('breached');
    expect(body.sla.resolution_status).toBe('missed');
    expect(body.sla.level).toBe('normal');
    // Sessão histórica veio no current_session.
    expect(body.current_session?.id).toBe(SESSION_ID);
    // Responsável derivado do closed_by_user_id da sessão (assigned_user_id era null).
    expect(body.assignee?.id).toBe(OPERATOR_ID);
    expect(body.assignee?.name).toBe('Op Erador');
  });

  it('master_admin sem company_id => 400', async () => {
    session = masterAdminSession();
    fake = createFakeSupabase();
    const { GET } = await import('@/app/api/admin/conversations/[id]/details/route');
    const res = await GET(getReq(`http://t/api/admin/conversations/${CONV_ID}/details`) as any, {
      params: Promise.resolve({ id: CONV_ID }),
    });
    expect(res.status).toBe(400);
  });

  it('conversa inexistente => 404', async () => {
    fake = createFakeSupabase({
      tables: { conversations: { selectResults: [{ data: null }] } },
    });
    const { GET } = await import('@/app/api/admin/conversations/[id]/details/route');
    const res = await GET(getReq(`http://t/api/admin/conversations/${CONV_ID}/details`) as any, {
      params: Promise.resolve({ id: CONV_ID }),
    });
    expect(res.status).toBe(404);
  });
});

// ========================================================================== //
// GET /conversations (lista enriquecida §9.1 + §6.1 + §12.3)
// ========================================================================== //
function listRow(extra: Record<string, unknown>) {
  return {
    id: extra.id,
    company_id: COMPANY_ID,
    agent_id: AGENT_ID,
    session_id: null,
    status: 'open',
    channel: 'web',
    user_id: null,
    user_name: 'C',
    user_phone: null,
    user_avatar: null,
    agent_name: 'Smith',
    last_message_preview: 'p',
    last_message_at: '2026-06-21T10:00:00Z',
    unread_count: 0,
    status_color: 'green',
    assigned_user_id: null,
    current_attendance_session_id: null,
    sla_priority: null,
    last_customer_message_at: null,
    last_human_message_at: null,
    last_ai_message_at: null,
    customer_waiting_since: null,
    agent_paused: false,
    created_at: '2026-06-21T09:00:00Z',
    agents: { id: AGENT_ID, name: 'Smith' },
    ...extra,
  };
}

describe('GET /conversations (§9.1 + §6.1 + §12.3)', () => {
  it('preserva chave `conversations`, pagina e ordena por prioridade §6.1', async () => {
    // 4 conversas: open(antiga), HUMAN_ACTIVE, HUMAN_REQUESTED, open recente.
    fake = createFakeSupabase({
      tables: {
        conversations: {
          selectResults: [
            {
              data: [
                listRow({ id: 'c-open-old', status: 'open', last_message_at: '2026-06-21T08:00:00Z' }),
                listRow({ id: 'c-active', status: 'HUMAN_ACTIVE', last_message_at: '2026-06-21T09:00:00Z' }),
                listRow({ id: 'c-req', status: 'HUMAN_REQUESTED', last_message_at: '2026-06-21T07:00:00Z' }),
                listRow({ id: 'c-open-new', status: 'open', last_message_at: '2026-06-21T11:00:00Z' }),
              ],
            },
          ],
        },
      },
    });
    const { GET } = await import('@/app/api/admin/conversations/route');
    const res = await GET(getReq('http://t/api/admin/conversations') as any);
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(Array.isArray(body.conversations)).toBe(true);
    expect(body.pagination).toBeDefined();
    const order = body.conversations.map((c: { id: string }) => c.id);
    // HUMAN_REQUESTED primeiro; depois HUMAN_ACTIVE; depois os 'open' por
    // last_message_at desc (novo antes do antigo).
    expect(order[0]).toBe('c-req');
    expect(order[1]).toBe('c-active');
    expect(order.indexOf('c-open-new')).toBeLessThan(order.indexOf('c-open-old'));
  });

  it('filtro status=human consulta os 3 estados humanos (sem 500)', async () => {
    fake = createFakeSupabase({
      tables: {
        conversations: {
          selectResults: [{ data: [listRow({ id: 'c1', status: 'PENDING_CUSTOMER' })] }],
        },
      },
    });
    const { GET } = await import('@/app/api/admin/conversations/route');
    const res = await GET(getReq('http://t/api/admin/conversations?status=human') as any);
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.conversations).toHaveLength(1);
  });

  it('master_admin sem company_id => 400', async () => {
    session = masterAdminSession();
    fake = createFakeSupabase();
    const { GET } = await import('@/app/api/admin/conversations/route');
    const res = await GET(getReq('http://t/api/admin/conversations') as any);
    expect(res.status).toBe(400);
  });

  it('master_admin COM company_id na query => 200 e escopa pela company', async () => {
    session = masterAdminSession();
    fake = createFakeSupabase({
      tables: {
        conversations: {
          selectResults: [{ data: [listRow({ id: 'c-master', status: 'open' })] }],
        },
      },
    });
    const { GET } = await import('@/app/api/admin/conversations/route');
    const res = await GET(
      getReq(`http://t/api/admin/conversations?company_id=${COMPANY_ID}`) as any,
    );
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.conversations).toHaveLength(1);
    expect(body.conversations[0].id).toBe('c-master');
    // a leitura é escopada por company_id (sem update direto de status).
    expect(fake.conversationsStatusUpdates).toHaveLength(0);
  });

  it('channel=widget retorna só widget (widget != web, §12.3)', async () => {
    // O filtro de canal é aplicado no banco (query.eq('channel', 'widget')); o
    // fake não filtra, então configuramos o selectResult já com a linha widget.
    fake = createFakeSupabase({
      tables: {
        conversations: {
          selectResults: [{ data: [listRow({ id: 'c-widget', channel: 'widget' })] }],
        },
      },
    });
    const { GET } = await import('@/app/api/admin/conversations/route');
    const res = await GET(getReq('http://t/api/admin/conversations?channel=widget') as any);
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.conversations).toHaveLength(1);
    expect(body.conversations[0].channel).toBe('widget');
    expect(body.conversations[0].channel).not.toBe('web');
  });

  it('sla_status=breached filtra por health enriquecido', async () => {
    const SESSION_A = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
    const SESSION_B = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
    fake = createFakeSupabase({
      tables: {
        conversations: {
          selectResults: [
            {
              data: [
                listRow({
                  id: 'c-breached',
                  status: 'HUMAN_ACTIVE',
                  current_attendance_session_id: SESSION_A,
                }),
                listRow({
                  id: 'c-ok',
                  status: 'HUMAN_ACTIVE',
                  current_attendance_session_id: SESSION_B,
                }),
              ],
            },
          ],
        },
        // 1ª leitura desta tabela = enriquecimento de SLA por sessão.
        attendance_sla: {
          selectResults: [
            {
              data: [
                {
                  attendance_session_id: SESSION_A,
                  health_status: 'breached',
                  sla_level: 'standard',
                  first_response_deadline: null,
                  resolution_deadline: null,
                },
                {
                  attendance_session_id: SESSION_B,
                  health_status: 'within_sla',
                  sla_level: 'standard',
                  first_response_deadline: null,
                  resolution_deadline: null,
                },
              ],
            },
          ],
        },
        conversation_inactivity_timers: { selectResults: [{ data: [] }] },
      },
    });
    const { GET } = await import('@/app/api/admin/conversations/route');
    const res = await GET(
      getReq('http://t/api/admin/conversations?sla_status=breached') as any,
    );
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.conversations).toHaveLength(1);
    expect(body.conversations[0].id).toBe('c-breached');
    expect(body.conversations[0].sla_health_status).toBe('breached');
  });

  it('prioriza SLA breached/critical (rank 3) acima de HUMAN_ACTIVE (rank 2)', async () => {
    const SESSION_BREACHED = 'cccccccc-cccc-cccc-cccc-cccccccccccc';
    fake = createFakeSupabase({
      tables: {
        conversations: {
          selectResults: [
            {
              data: [
                // HUMAN_ACTIVE recente (rank 2), SEM SLA urgente.
                listRow({
                  id: 'c-active-recent',
                  status: 'HUMAN_ACTIVE',
                  last_message_at: '2026-06-21T12:00:00Z',
                }),
                // open com SLA breached (rank 3) e mensagem MAIS ANTIGA: ainda assim
                // deve vir ANTES da HUMAN_ACTIVE por causa do rank de prioridade.
                listRow({
                  id: 'c-breached-old',
                  status: 'open',
                  last_message_at: '2026-06-21T06:00:00Z',
                  current_attendance_session_id: SESSION_BREACHED,
                }),
              ],
            },
          ],
        },
        attendance_sla: {
          selectResults: [
            {
              data: [
                {
                  attendance_session_id: SESSION_BREACHED,
                  health_status: 'breached',
                  sla_level: 'standard',
                  first_response_deadline: null,
                  resolution_deadline: null,
                },
              ],
            },
          ],
        },
        conversation_inactivity_timers: { selectResults: [{ data: [] }] },
      },
    });
    const { GET } = await import('@/app/api/admin/conversations/route');
    const res = await GET(getReq('http://t/api/admin/conversations') as any);
    expect(res.status).toBe(200);
    const body = await res.json();
    const order = body.conversations.map((c: { id: string }) => c.id);
    expect(order[0]).toBe('c-breached-old');
    expect(order[1]).toBe('c-active-recent');
  });
});
