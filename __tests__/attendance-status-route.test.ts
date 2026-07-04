/**
 * §18.1 — Teste da ROTA LEGADA de status (PUT /api/admin/conversations/status).
 *
 * Este SHIM legado (D1/§8.1) NUNCA pode voltar a gravar `conversations.status`
 * direto: ele valida o status-alvo contra a máquina de estados (§6.3), mapeia
 * status → ação e chama a MESMA RPC transacional única. Os dois comportamentos
 * load-bearing que este teste prova:
 *
 *  (a) status DESCONHECIDO/não-acionável -> 400 SEM chamar a RPC (callTransition)
 *      nem fazer update direto em conversations;
 *  (b) status ACIONÁVEL (ex.: HUMAN_REQUESTED) -> chama callTransition (a RPC)
 *      com a ação mapeada e NÃO faz `from('conversations').update(...)` direto.
 *
 * `mapStatusToAction` (módulo separado attendance-status-map) roda DE VERDADE —
 * só os helpers de auth/conversa/RPC (attendance-actions) são mockados, com um
 * `callTransition` spy e o fake supabase compartilhado dos demais __tests__ para
 * provar a ausência de update direto.
 *
 * Runner ATIVO: `npm test` (vitest). Veja vitest.config.ts.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createFakeSupabase, type FakeSupabase } from './helpers/fake-supabase';
import type { AdminSessionData } from '@/lib/iron-session';

// --- Estado de mocks compartilhado entre os testes ---------------------------
let fake: FakeSupabase;
let session: AdminSessionData;

const COMPANY_ID = '11111111-1111-1111-1111-111111111111';
const CONV_ID = '22222222-2222-2222-2222-222222222222';
const AGENT_ID = '33333333-3333-3333-3333-333333333333';
const COMPANY_ADMIN_ID = '44444444-4444-4444-4444-444444444444';

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

// Proxy estável para o fake supabase: `resolveConversation` (real) usaria o
// client, mas aqui mockamos attendance-actions inteiro, então o fake só serve
// para PROVAR que nenhum update direto de conversations.status acontece.
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

// Spies para os helpers de attendance-actions usados pela rota de status.
const callTransition = vi.fn();
const logLegacyStatusWriteWarning = vi.fn();
const resolveConversation = vi.fn();
const requireAttendanceAdmin = vi.fn();

vi.mock('@/lib/attendance-actions', () => ({
  callTransition: (...args: unknown[]) => callTransition(...args),
  logLegacyStatusWriteWarning: (...args: unknown[]) => logLegacyStatusWriteWarning(...args),
  resolveConversation: (...args: unknown[]) => resolveConversation(...args),
  requireAttendanceAdmin: (...args: unknown[]) => requireAttendanceAdmin(...args),
}));

function putReq(body: unknown): Request {
  return new Request('http://t/api/admin/conversations/status', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  session = companyAdminSession();
  fake = createFakeSupabase();

  // Auth OK por padrão (company_admin com company_id).
  requireAttendanceAdmin.mockResolvedValue({
    auth: {
      session,
      companyId: COMPANY_ID,
      actorUserId: COMPANY_ADMIN_ID,
      actorMetadata: { actor_type: 'human', actor_user_id: COMPANY_ADMIN_ID },
    },
  });
  // Conversa resolvida por padrão.
  resolveConversation.mockResolvedValue({ conversation: convRow() });
  // RPC OK por padrão.
  callTransition.mockResolvedValue({ result: { status: 'HUMAN_REQUESTED' } });
});

describe('PUT /api/admin/conversations/status (§8.1/§18.1) — shim legado', () => {
  it('(a) status DESCONHECIDO -> 400 SEM chamar a RPC nem update direto', async () => {
    const { PUT } = await import('@/app/api/admin/conversations/status/route');
    const res = await PUT(putReq({ conversation_id: CONV_ID, status: 'WAT_IS_THIS' }) as any);

    expect(res.status).toBe(400);
    // NÃO chama a RPC transacional.
    expect(callTransition).not.toHaveBeenCalled();
    // NÃO resolve/toca conversa (rejeita antes de qualquer leitura).
    expect(resolveConversation).not.toHaveBeenCalled();
    // NÃO faz nenhum update direto (em conversations ou qualquer tabela).
    expect(fake.conversationsStatusUpdates).toHaveLength(0);
    expect(fake.writes).toHaveLength(0);
    expect(fake.rpcCalls).toHaveLength(0);
  });

  it('(a2) PENDING_CUSTOMER (derivado, não-acionável) -> 400 SEM RPC nem update', async () => {
    const { PUT } = await import('@/app/api/admin/conversations/status/route');
    const res = await PUT(putReq({ conversation_id: CONV_ID, status: 'PENDING_CUSTOMER' }) as any);

    expect(res.status).toBe(400);
    expect(callTransition).not.toHaveBeenCalled();
    expect(resolveConversation).not.toHaveBeenCalled();
    expect(fake.conversationsStatusUpdates).toHaveLength(0);
    expect(fake.writes).toHaveLength(0);
  });

  it('(b) status ACIONÁVEL (HUMAN_REQUESTED) -> chama callTransition; sem update direto', async () => {
    const { PUT } = await import('@/app/api/admin/conversations/status/route');
    const res = await PUT(putReq({ conversation_id: CONV_ID, status: 'HUMAN_REQUESTED' }) as any);

    expect(res.status).toBe(200);
    // Chama a RPC transacional única com a ação mapeada (request_handoff).
    expect(callTransition).toHaveBeenCalledTimes(1);
    const callArgs = callTransition.mock.calls[0][1] as Record<string, unknown>;
    expect(callArgs.action).toBe('request_handoff');
    expect(callArgs.actorType).toBe('human');
    expect(callArgs.conversationId).toBe(CONV_ID);
    // D1/§8.1: NENHUM update direto de conversations.status.
    expect(fake.conversationsStatusUpdates).toHaveLength(0);
    expect(
      fake.writes.filter((w) => w.table === 'conversations' && w.op === 'update'),
    ).toHaveLength(0);
  });

  it('(b2) CLOSED -> mapeia para a ação close via callTransition', async () => {
    callTransition.mockResolvedValue({ result: { status: 'CLOSED' } });
    const { PUT } = await import('@/app/api/admin/conversations/status/route');
    const res = await PUT(putReq({ conversation_id: CONV_ID, status: 'CLOSED' }) as any);

    expect(res.status).toBe(200);
    expect(callTransition).toHaveBeenCalledTimes(1);
    const callArgs = callTransition.mock.calls[0][1] as Record<string, unknown>;
    expect(callArgs.action).toBe('close');
    expect(fake.conversationsStatusUpdates).toHaveLength(0);
  });

  it('campos obrigatórios ausentes -> 400 SEM RPC', async () => {
    const { PUT } = await import('@/app/api/admin/conversations/status/route');
    const res = await PUT(putReq({ conversation_id: CONV_ID }) as any);

    expect(res.status).toBe(400);
    expect(callTransition).not.toHaveBeenCalled();
    expect(fake.writes).toHaveLength(0);
  });
});
