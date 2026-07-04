/**
 * S9 — Testes de LÓGICA PURA do frontend de atendimento (SPRINTS S9, §18.1).
 *
 * Runner: vitest em ambiente `node` (ver vitest.config.ts) — este repo NÃO tem
 * jsdom/@testing-library, então seguimos o padrão dos demais testes (regras puras
 * extraídas dos componentes/hooks). Cobre:
 *
 *  - SlaIndicator calcula o status visual correto + "Sem SLA configurado"
 *    (via `computeSlaVisual` de lib/sla-visual);
 *  - hook de polling (admin) aplica backoff em erro e reseta em sucesso, e
 *    constrói a query da lista (`nextPollDelay`, `buildListQuery`);
 *  - hook de polling do WIDGET aplica backoff sem novidade e reseta com novidade
 *    (`nextWidgetPollDelay`);
 *  - ConversationDetailsPanel: transições permitidas por status (§6.3) e labels
 *    (§6.1) — garante que os botões certos habilitam/desabilitam, e que o módulo
 *    importa sem quebrar (cobre os estados loading/erro/vazio que dependem desses
 *    helpers + imports do componente).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  computeSlaVisual,
  firstResponseLabel,
  normalizeSlaHealth,
  resolutionLabel,
  slaLevelLabel,
  SLA_SEVERITY,
} from '@/lib/sla-visual';
import { buildListQuery, DEFAULT_BACKOFF, nextPollDelay } from '@/hooks/use-conversation-polling';
import { nextWidgetPollDelay } from '@/hooks/use-widget-polling';
import { allowedActions, statusLabel } from '@/components/chat/ConversationDetailsPanel';
import { messageIsHuman } from '@/types/conversation-details';
import type { SlaSnapshot } from '@/types/conversation-details';
import { createFakeSupabase, type FakeSupabase } from './helpers/fake-supabase';
import type { AdminSessionData } from '@/lib/iron-session';

// =========================================================================== //
// Mocks de rota — GET /api/messages projeta `is_human` (fonte única, §22 item 3)
//
// O módulo da rota faz `const supabaseAdmin = getSupabaseAdmin()` no load, então
// usamos o mesmo proxy estável do attendance-s7-routes para apontar para o fake
// reconstruído a cada teste. `getUserOrAdminSession` é mockado como admin válido.
// =========================================================================== //
let routeFake: FakeSupabase;
let routeAdminSession: AdminSessionData;
// Sessão de USUÁRIO (iron-session) consumida por GET /api/conversations.
let routeUserId: string | null;

const stableMessagesClientProxy = new Proxy(
  {},
  {
    get(_t, prop) {
      const target = routeFake.client as Record<string | symbol, unknown>;
      const value = target[prop];
      return typeof value === 'function' ? value.bind(target) : value;
    },
  },
);
vi.mock('@/lib/supabase-admin', () => ({
  getSupabaseAdmin: () => stableMessagesClientProxy,
}));
vi.mock('@/lib/auth-actions', () => ({
  getUserOrAdminSession: async () => ({ adminSession: routeAdminSession }),
}));
// GET /api/conversations cria o client via `createClient(@supabase/supabase-js)`
// e autentica via `getIronSession(iron-session)` + `cookies(next/headers)`.
// Reapontamos os três para o mesmo fake estável usado pelos demais testes de rota.
vi.mock('@supabase/supabase-js', () => ({
  createClient: () => stableMessagesClientProxy,
}));
vi.mock('next/headers', () => ({
  cookies: async () => ({}),
}));
vi.mock('iron-session', () => ({
  getIronSession: async () => ({ userId: routeUserId }),
}));

// =========================================================================== //
// SlaIndicator — status visual (§12.2)
// =========================================================================== //

describe('S9 SlaIndicator / computeSlaVisual', () => {
  const base: SlaSnapshot = {
    health_status: 'within_sla',
    first_response_status: 'pending',
    resolution_status: 'pending',
    level: 'high',
    first_response_deadline: null,
    resolution_deadline: null,
    first_response_at: null,
    resolved_at: null,
  };

  it('mapeia cada health_status para o tom/rótulo corretos', () => {
    expect(computeSlaVisual({ ...base, health_status: 'within_sla' })).toMatchObject({
      status: 'within_sla',
      tone: 'success',
    });
    expect(computeSlaVisual({ ...base, health_status: 'at_risk' })).toMatchObject({
      status: 'at_risk',
      tone: 'warning',
    });
    expect(computeSlaVisual({ ...base, health_status: 'critical' })).toMatchObject({
      status: 'critical',
      tone: 'danger',
    });
    expect(computeSlaVisual({ ...base, health_status: 'breached' })).toMatchObject({
      status: 'breached',
      tone: 'danger',
    });
    expect(computeSlaVisual({ ...base, health_status: 'paused' })).toMatchObject({
      status: 'paused',
      tone: 'muted',
    });
  });

  it("'none' => 'Sem SLA configurado' e isNone=true", () => {
    const v = computeSlaVisual({ ...base, health_status: 'none', level: null });
    expect(v.isNone).toBe(true);
    expect(v.label).toBe('Sem SLA configurado');
    expect(v.levelLabel).toBeNull();
  });

  it('snapshot ausente (conversa antiga, §22 item 5) => none', () => {
    expect(computeSlaVisual(null).status).toBe('none');
    expect(computeSlaVisual(undefined).isNone).toBe(true);
  });

  it('normaliza health_status desconhecido para none', () => {
    expect(normalizeSlaHealth('foo')).toBe('none');
    expect(normalizeSlaHealth(null)).toBe('none');
    expect(normalizeSlaHealth('breached')).toBe('breached');
  });

  it('rotula nível e marcos de SLA', () => {
    expect(slaLevelLabel('normal')).toBe('Normal');
    expect(slaLevelLabel('high')).toBe('Alta');
    expect(slaLevelLabel('critical')).toBe('Crítica');
    expect(slaLevelLabel(null)).toBeNull();
    expect(firstResponseLabel('met')).toBe('Cumprida');
    expect(resolutionLabel('breached')).toBe('Vencida');
    expect(resolutionLabel(null)).toBe('Pendente');
  });

  it('severidade ordena breached acima de within_sla e none no fundo', () => {
    expect(SLA_SEVERITY.breached).toBeGreaterThan(SLA_SEVERITY.within_sla);
    expect(SLA_SEVERITY.critical).toBeGreaterThan(SLA_SEVERITY.at_risk);
    expect(SLA_SEVERITY.none).toBe(0);
  });
});

// =========================================================================== //
// Polling admin — backoff + query
// =========================================================================== //

describe('S9 useConversationListPolling helpers', () => {
  it('sucesso reseta para o intervalo base', () => {
    expect(nextPollDelay(30000, true)).toBe(DEFAULT_BACKOFF.baseIntervalMs);
  });

  it('erro aplica backoff exponencial limitado ao teto', () => {
    const d1 = nextPollDelay(DEFAULT_BACKOFF.baseIntervalMs, false);
    expect(d1).toBe(DEFAULT_BACKOFF.baseIntervalMs * DEFAULT_BACKOFF.factor);
    const d2 = nextPollDelay(d1, false);
    expect(d2).toBeGreaterThan(d1);
    // converge ao teto e não ultrapassa
    let d = d2;
    for (let i = 0; i < 10; i++) d = nextPollDelay(d, false);
    expect(d).toBe(DEFAULT_BACKOFF.maxIntervalMs);
  });

  it('buildListQuery omite filtros "all"/vazios e serializa os ativos (§12.3)', () => {
    expect(buildListQuery(undefined)).toBe('');
    expect(buildListQuery({ channel: 'all', status: 'all' })).toBe('');
    const q = buildListQuery({ channel: 'widget', status: 'human', search: 'ana' });
    expect(q.startsWith('?')).toBe(true);
    const sp = new URLSearchParams(q.slice(1));
    expect(sp.get('channel')).toBe('widget');
    expect(sp.get('status')).toBe('human');
    expect(sp.get('search')).toBe('ana');
  });
});

// =========================================================================== //
// Polling WIDGET — sem subscription anon (§17 item 7, §18.2)
// =========================================================================== //

describe('S9 useWidgetPolling backoff', () => {
  it('novidade reseta para 3s; sem novidade dobra até 30s', () => {
    expect(nextWidgetPollDelay(30000, true)).toBe(3000);
    expect(nextWidgetPollDelay(3000, false)).toBe(6000);
    let d = 3000;
    for (let i = 0; i < 10; i++) d = nextWidgetPollDelay(d, false);
    expect(d).toBe(30000);
  });
});

// =========================================================================== //
// messageIsHuman — FONTE ÚNICA da autoria humana na leitura (§22 item 3)
//
// Projetada como `is_human` por GET /api/messages e consumida pela timeline
// admin/dashboard, em vez de cada consumidor reimplementar a regra (e divergir).
// =========================================================================== //

describe('S9 messageIsHuman (fonte única de is_human)', () => {
  it('mensagem humana LEGADA (role=assistant + sender_user_id, sem JOIN/prefixo) é humana', () => {
    // Caso exato do bug: persistida pré-backfill, sem author_type, sem `sender`
    // populado pelo JOIN e sem prefixo [👤]. Antes era renderizada como IA.
    expect(
      messageIsHuman({
        role: 'assistant',
        author_type: null,
        sender_user_id: 'user-123',
      }),
    ).toBe(true);
  });

  it('author_type=human_operator é humano (caminho novo / backfill)', () => {
    expect(
      messageIsHuman({ role: 'assistant', author_type: 'human_operator', sender_user_id: null }),
    ).toBe(true);
  });

  it('role=user é sempre o CLIENTE (nunca humano-operador)', () => {
    expect(
      messageIsHuman({ role: 'user', author_type: null, sender_user_id: 'user-123' }),
    ).toBe(false);
  });

  it('role=user prevalece sobre author_type=human_operator (cliente nunca é operador)', () => {
    // Guard de role='user' avaliado ANTES de author_type: dado inconsistente
    // (cliente marcado como human_operator) ainda classifica como CLIENTE.
    expect(
      messageIsHuman({ role: 'user', author_type: 'human_operator', sender_user_id: null }),
    ).toBe(false);
  });

  it('ai_agent/system/assistant puro NÃO são humanos', () => {
    expect(messageIsHuman({ role: 'assistant', author_type: 'ai_agent', sender_user_id: null })).toBe(
      false,
    );
    expect(messageIsHuman({ role: 'assistant', author_type: 'system', sender_user_id: null })).toBe(
      false,
    );
    expect(messageIsHuman({ role: 'assistant', sender_user_id: null })).toBe(false);
  });
});

// =========================================================================== //
// GET /api/messages — projeção do campo `is_human` no payload (§22 item 3)
//
// Trava a regressão original: a rota DEVE projetar `is_human` derivado de
// role/author_type/sender_user_id, para que os consumidores (timeline admin/
// dashboard) consumam o campo único em vez de reimplementar a regra. Sem este
// teste, remover a projeção (route.ts:155-162) não falharia nada e a mensagem
// humana legada voltaria a ser exibida como IA.
// =========================================================================== //

const MSG_COMPANY_ID = '11111111-1111-1111-1111-111111111111';
const MSG_CONV_ID = '22222222-2222-2222-2222-222222222222';

function msgAdminSession(): AdminSessionData {
  return {
    adminId: '44444444-4444-4444-4444-444444444444',
    email: 'admin@co.com',
    name: 'Admin',
    role: 'company_admin',
    companyId: MSG_COMPANY_ID,
    expiresAt: new Date(Date.now() + 3600_000).toISOString(),
  };
}

describe('S9 GET /api/messages projeta is_human (fonte única)', () => {
  beforeEach(() => {
    routeAdminSession = msgAdminSession();
  });

  it('mensagem humana LEGADA (role=assistant + sender_user_id, sem author_type/JOIN) => is_human=true', async () => {
    routeFake = createFakeSupabase({
      tables: {
        // 1ª leitura: validateConversationAccess (conversations)
        conversations: {
          selectResults: [
            { data: { id: MSG_CONV_ID, user_id: null, company_id: MSG_COMPANY_ID } },
          ],
        },
        // 2ª leitura: mensagens da conversa
        messages: {
          selectResults: [
            {
              data: [
                {
                  id: 'm-legacy',
                  role: 'assistant',
                  content: 'oi, aqui é a Ana',
                  author_type: null,
                  sender_user_id: 'user-123',
                  sender: null,
                },
                {
                  id: 'm-ai',
                  role: 'assistant',
                  content: 'resposta da IA',
                  author_type: null,
                  sender_user_id: null,
                  sender: null,
                },
                {
                  id: 'm-customer',
                  role: 'user',
                  content: 'preciso de ajuda',
                  author_type: null,
                  sender_user_id: null,
                  sender: null,
                },
              ],
            },
          ],
        },
      },
    });

    const { GET } = await import('@/app/api/messages/route');
    const res = await GET(
      new Request(`http://t/api/messages?conversation_id=${MSG_CONV_ID}`) as any,
    );
    expect(res.status).toBe(200);
    const body = await res.json();
    const byId: Record<string, any> = Object.fromEntries(
      body.messages.map((m: any) => [m.id, m]),
    );
    // O caso exato do bug: legada vira HUMANA, não IA.
    expect(byId['m-legacy'].is_human).toBe(true);
    // IA pura e cliente NÃO são humanos-operadores.
    expect(byId['m-ai'].is_human).toBe(false);
    expect(byId['m-customer'].is_human).toBe(false);
  });

  it('author_type=human_operator (backfill) => is_human=true', async () => {
    routeFake = createFakeSupabase({
      tables: {
        conversations: {
          selectResults: [
            { data: { id: MSG_CONV_ID, user_id: null, company_id: MSG_COMPANY_ID } },
          ],
        },
        messages: {
          selectResults: [
            {
              data: [
                {
                  id: 'm-backfill',
                  role: 'assistant',
                  content: 'operador',
                  author_type: 'human_operator',
                  sender_user_id: null,
                  sender: null,
                },
              ],
            },
          ],
        },
      },
    });

    const { GET } = await import('@/app/api/messages/route');
    const res = await GET(
      new Request(`http://t/api/messages?conversation_id=${MSG_CONV_ID}`) as any,
    );
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.messages[0].is_human).toBe(true);
  });
});

// =========================================================================== //
// GET /api/conversations?session_id=… — projeção de `is_human` no LOAD INICIAL
//
// Espelha o teste de GET /api/messages: o carregamento inicial do dashboard
// (route.ts:171-178) também DEVE projetar `is_human` como fonte única, senão a
// mensagem humana legada (role='assistant'+sender_user_id) seria exibida como IA
// no load e o polling por /api/messages não corrigiria (só mescla ids novos).
// =========================================================================== //

const CONV_USER_ID = '55555555-5555-5555-5555-555555555555';

describe('S9 GET /api/conversations projeta is_human (load inicial)', () => {
  beforeEach(() => {
    routeUserId = CONV_USER_ID;
  });

  it('legada => is_human=true; IA e cliente => is_human=false', async () => {
    routeFake = createFakeSupabase({
      tables: {
        // company_id do usuário (espelha o padrão dos demais testes de rota).
        users_v2: {
          selectResults: [{ data: { company_id: MSG_COMPANY_ID } }],
        },
        // 1ª leitura do path session_id: conversa via .maybeSingle().
        conversations: {
          selectResults: [
            {
              data: {
                id: MSG_CONV_ID,
                agent_id: null,
                session_id: 'sess-1',
                status: 'open',
                title: 'Conversa',
                created_at: '2026-06-21T00:00:00Z',
                updated_at: '2026-06-21T00:00:00Z',
              },
            },
          ],
        },
        // 2ª leitura: mensagens da conversa.
        messages: {
          selectResults: [
            {
              data: [
                {
                  id: 'm-legacy',
                  role: 'assistant',
                  content: 'oi, aqui é a Ana',
                  author_type: null,
                  sender_user_id: 'user-123',
                  sender: null,
                },
                {
                  id: 'm-ai',
                  role: 'assistant',
                  content: 'resposta da IA',
                  author_type: 'ai_agent',
                  sender_user_id: null,
                  sender: null,
                },
                {
                  id: 'm-customer',
                  role: 'user',
                  content: 'preciso de ajuda',
                  author_type: null,
                  sender_user_id: null,
                  sender: null,
                },
              ],
            },
          ],
        },
      },
    });

    const { GET } = await import('@/app/api/conversations/route');
    const res = await GET(new Request('http://t/api/conversations?session_id=sess-1') as any);
    expect(res.status).toBe(200);
    const body = await res.json();
    const byId: Record<string, any> = Object.fromEntries(
      body.messages.map((m: any) => [m.id, m]),
    );
    // O caso exato do bug: legada vira HUMANA no load inicial.
    expect(byId['m-legacy'].is_human).toBe(true);
    // IA e cliente NÃO são humanos-operadores.
    expect(byId['m-ai'].is_human).toBe(false);
    expect(byId['m-customer'].is_human).toBe(false);
  });
});

// =========================================================================== //
// ConversationDetailsPanel — transições (§6.3) + labels (§6.1)
// =========================================================================== //

describe('S9 ConversationDetailsPanel logic', () => {
  it('labels canônicos dos 3 estados humanos (§6.1)', () => {
    expect(statusLabel('HUMAN_REQUESTED')).toBe('Aguardando humano');
    expect(statusLabel('HUMAN_ACTIVE')).toBe('Humano ativo');
    expect(statusLabel('PENDING_CUSTOMER')).toBe('Aguardando cliente');
  });

  it('open permite claim/resolve/close, não return_to_ai', () => {
    const a = allowedActions('open', false);
    expect(a.has('claim')).toBe(true);
    expect(a.has('resolve')).toBe(true);
    expect(a.has('close')).toBe(true);
    expect(a.has('return_to_ai')).toBe(false);
  });

  it('HUMAN_ACTIVE permite return_to_ai/resolve/close, não claim', () => {
    const a = allowedActions('HUMAN_ACTIVE', false);
    expect(a.has('return_to_ai')).toBe(true);
    expect(a.has('claim')).toBe(false);
  });

  it('HUMAN_REQUESTED permite claim e return_to_ai', () => {
    const a = allowedActions('HUMAN_REQUESTED', false);
    expect(a.has('claim')).toBe(true);
    expect(a.has('return_to_ai')).toBe(true);
  });

  it('RESOLVED/CLOSED são terminais: só reenviar + SLA, sem claim/return/close', () => {
    for (const s of ['RESOLVED', 'CLOSED']) {
      const a = allowedActions(s, false);
      expect(a.has('resend')).toBe(true);
      expect(a.has('claim')).toBe(false);
      expect(a.has('return_to_ai')).toBe(false);
      expect(a.has('close')).toBe(false);
      expect(a.has('resolve')).toBe(false);
    }
  });

  it('SLA pausado expõe resume_sla; ativo expõe pause_sla', () => {
    expect(allowedActions('open', true).has('resume_sla')).toBe(true);
    expect(allowedActions('open', true).has('pause_sla')).toBe(false);
    expect(allowedActions('open', false).has('pause_sla')).toBe(true);
  });

  it('status desconhecido cai no rótulo cru (sem quebrar)', () => {
    expect(statusLabel('SOMETHING_NEW')).toBe('SOMETHING_NEW');
    expect(statusLabel(null)).toBe('—');
  });
});
