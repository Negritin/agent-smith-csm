/**
 * BFF route.ts — branch Evolution + estreitamento da whitelist (write path) +
 * token de webhook por tenant (§1.1/§4.1/§7).
 *
 * Prova os comportamentos load-bearing desta sprint sobre
 * `POST`/`GET /api/admin/integrations`:
 *
 *  (1) WHITELIST ESTREITA: só {z-api, uazapi, evolution} (+ 'none') passam. Os
 *      aliases órfãos sem bridge (evolution-api/meta/wppconnect/whatsapp-cloud)
 *      retornam 400 'Provider inválido' SEM nenhuma escrita.
 *  (2) BRANCH EVOLUTION (correção de bug): evolution EXIGE base_url e instance_id
 *      (400 em português, espelhando o uazapi) e NÃO herda o default Z-API de
 *      base_url; quando válido persiste client_token=null e o base_url informado.
 *  (3) Tipagem snake_case persistida (provider/identifier/token/instance_id/
 *      base_url/client_token) batendo 1:1 com as colunas de `integrations`.
 *  (4) TOKEN DE WEBHOOK (§7): o INSERT inclui o quarteto `webhook_token*` gerado
 *      SERVER-SIDE no formato pinado `wh_{tag}_{...}` (tag por provider); um
 *      UPDATE benigno (linha já COM hash) NÃO regenera o token (URL viva
 *      preservada); o UPDATE de linha legada SEM hash CURA (heal) gerando o
 *      quarteto; o GET projeta EXPLICITAMENTE token/client_token + webhook_token*;
 *      e o guard cross-tenant fecha sem ler/escrever a linha do outro tenant.
 *
 * O fake supabase compartilhado dos demais __tests__ grava as escritas; só
 * auth/security-audit e o createClient do supabase-js são mockados. Runner ATIVO:
 * `npm test` (vitest). Veja vitest.config.ts.
 */
import { NextRequest } from 'next/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createFakeSupabase, type FakeSupabase } from './helpers/fake-supabase';
import type { AdminSessionData } from '@/lib/iron-session';

const COMPANY_ID = '11111111-1111-1111-1111-111111111111';
const OTHER_COMPANY_ID = '22222222-2222-2222-2222-222222222222';
const AGENT_ID = '33333333-3333-3333-3333-333333333333';
const COMPANY_ADMIN_ID = '44444444-4444-4444-4444-444444444444';
const Z_API_DEFAULT_BASE_URL = 'https://api.z-api.io/instances';

// Os 4 campos do token de webhook gerados SERVER-SIDE (§1.1). PINADO no CONTRATO.
const WEBHOOK_TOKEN_FIELDS = [
  'webhook_token',
  'webhook_token_hash',
  'webhook_token_prefix',
  'webhook_token_rotated_at',
] as const;

// Tag de observabilidade por provider (§1.1) — PINADA (idêntica ao backfill e ao
// regenerate). Usada para asserir o prefixo do token gerado no INSERT.
const WEBHOOK_TOKEN_TAGS: Record<string, string> = {
  'z-api': 'zapi',
  uazapi: 'uaz',
  evolution: 'evo',
};

let fake: FakeSupabase;

// createClient é chamado no module-eval da rota (singleton supabaseAdmin). O
// proxy estável delega para o `fake` ATUAL, permitindo um fake fresco por teste.
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
vi.mock('@supabase/supabase-js', () => ({
  createClient: () => stableClientProxy,
}));

// Auth: company_admin com company_id por padrão (sem cross-tenant).
const requireAdminSession = vi.fn();
vi.mock('@/lib/auth-actions', () => ({
  requireAdminSession: () => requireAdminSession(),
}));

// Security-audit: no-op (não há cross-tenant nestes casos).
vi.mock('@/lib/security-audit', () => ({
  auditMasterAdminCompanyOverride: vi.fn(),
  logSecurityAudit: vi.fn(),
}));

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

/**
 * Configura o fake para um POST que chega no write path: o agente existe no
 * tenant e não há integração pré-existente (identifier livre + sem linha por
 * agente => INSERT). A escrita resolve com a linha persistida.
 */
function configureWritePath() {
  fake = createFakeSupabase({
    tables: {
      agents: {
        selectResults: [{ data: { id: AGENT_ID, company_id: COMPANY_ID } }],
      },
      integrations: {
        selectResults: [
          { data: [] }, // §6.3 passo 1 — lookup por identifier (sem conflito)
          { data: [] }, // §6.3 passo 2 — lookup por agente (sem existente => INSERT)
        ],
        writeResult: { data: { id: 'new-integration-id' }, error: null },
      },
    },
  });
}

/**
 * Configura o fake para um POST que cai no caminho de UPDATE: o agente existe e
 * JÁ HÁ uma integração por agente (existingByAgent != null) => UPDATE in-place.
 * `existingHash` controla o `webhook_token_hash` da linha existente:
 *   - string (linha viva com token)  => UPDATE benigno NÃO deve regenerar.
 *   - null   (linha legada sem token) => UPDATE deve CURAR (heal) gerando o quarteto.
 */
function configureUpdatePath(existingHash: string | null) {
  fake = createFakeSupabase({
    tables: {
      agents: {
        selectResults: [{ data: { id: AGENT_ID, company_id: COMPANY_ID } }],
      },
      integrations: {
        selectResults: [
          { data: [] }, // §6.3 passo 1 — lookup por identifier (sem conflito cross-tenant)
          {
            // §6.3 passo 2 — lookup por agente: existe => UPDATE in-place.
            data: [
              {
                id: 'existing-integration-id',
                provider: 'z-api',
                identifier: '5511999999999',
                company_id: COMPANY_ID,
                is_active: true,
                webhook_token_hash: existingHash,
              },
            ],
          },
        ],
        writeResult: { data: { id: 'existing-integration-id' }, error: null },
      },
    },
  });
}

/**
 * Configura o fake para um GET: o agente existe no tenant e a leitura da
 * integração devolve `row` (a projeção EXPLÍCITA de colunas).
 */
function configureGetPath(row: Record<string, unknown>) {
  fake = createFakeSupabase({
    tables: {
      agents: {
        selectResults: [{ data: { id: AGENT_ID, company_id: COMPANY_ID } }],
      },
      integrations: {
        selectResults: [{ data: [row] }],
      },
    },
  });
}

function postReq(body: unknown): NextRequest {
  return new NextRequest('http://t/api/admin/integrations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

function getReq(url: string): NextRequest {
  return new NextRequest(url, { method: 'GET' });
}

function lastInsert() {
  return fake.writes.find((w) => w.table === 'integrations' && w.op === 'insert');
}

function lastUpdate() {
  return fake.writes.find((w) => w.table === 'integrations' && w.op === 'update');
}

beforeEach(() => {
  vi.clearAllMocks();
  configureWritePath();
  requireAdminSession.mockResolvedValue({ session: companyAdminSession() });
});

describe('POST /api/admin/integrations — whitelist estreita', () => {
  it.each(['evolution-api', 'meta', 'wppconnect', 'whatsapp', 'whatsapp-cloud'])(
    'rejeita alias órfão "%s" com 400 e SEM escrita',
    async (provider) => {
      const { POST } = await import('@/app/api/admin/integrations/route');
      const res = await POST(
        postReq({
          agent_id: AGENT_ID,
          provider,
          identifier: '5511999999999',
          instance_id: 'inst-1',
          token: 'tok',
          base_url: 'https://evo.example.com',
        }),
      );

      expect(res.status).toBe(400);
      const body = await res.json();
      expect(body.error).toBe('Provider inválido');
      expect(fake.writes).toHaveLength(0);
    },
  );

  it.each(['z-api', 'uazapi', 'evolution', 'none'])(
    'aceita provider implementado "%s" na whitelist',
    async (provider) => {
      const { POST } = await import('@/app/api/admin/integrations/route');
      const res = await POST(
        postReq({
          agent_id: AGENT_ID,
          provider,
          identifier: '5511999999999',
          // Campos obrigatórios para uazapi/evolution; inócuos p/ z-api/none.
          instance_id: 'inst-1',
          token: 'tok',
          base_url: 'https://server.example.com',
        }),
      );

      // Passa da checagem de whitelist (não é 'Provider inválido').
      expect(res.status).toBe(200);
    },
  );
});

describe('POST /api/admin/integrations — branch evolution', () => {
  it('evolution SEM base_url retorna 400 e SEM escrita (sem default Z-API)', async () => {
    const { POST } = await import('@/app/api/admin/integrations/route');
    const res = await POST(
      postReq({
        agent_id: AGENT_ID,
        provider: 'evolution',
        identifier: '5511999999999',
        instance_id: 'inst-1',
        token: 'apikey-123',
        // base_url ausente
      }),
    );

    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.error).toBe('base_url é obrigatório para evolution');
    expect(fake.writes).toHaveLength(0);
  });

  it('evolution SEM instance_id retorna 400 e SEM escrita', async () => {
    const { POST } = await import('@/app/api/admin/integrations/route');
    const res = await POST(
      postReq({
        agent_id: AGENT_ID,
        provider: 'evolution',
        identifier: '5511999999999',
        token: 'apikey-123',
        base_url: 'https://evo.example.com',
        // instance_id ausente
      }),
    );

    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.error).toBe('instance_id é obrigatório para evolution');
    expect(fake.writes).toHaveLength(0);
  });

  it('evolution válido persiste snake_case: base_url informado (NÃO Z-API), client_token=null', async () => {
    const EVO_BASE_URL = 'https://evo.example.com';
    const { POST } = await import('@/app/api/admin/integrations/route');
    const res = await POST(
      postReq({
        agent_id: AGENT_ID,
        provider: 'evolution',
        identifier: '5511999999999', // connectedPhone
        instance_id: 'inst-1',
        token: 'apikey-123', // apikey
        client_token: 'deveria-ser-ignorado',
        base_url: EVO_BASE_URL,
      }),
    );

    expect(res.status).toBe(200);

    const insert = lastInsert();
    expect(insert).toBeDefined();
    const values = insert!.values as Record<string, unknown>;

    expect(values.provider).toBe('evolution');
    expect(values.identifier).toBe('5511999999999');
    expect(values.token).toBe('apikey-123');
    expect(values.instance_id).toBe('inst-1');
    // SEM default Z-API: persiste o servidor Evolution informado.
    expect(values.base_url).toBe(EVO_BASE_URL);
    expect(values.base_url).not.toBe(Z_API_DEFAULT_BASE_URL);
    // evolution não usa client_token (conceito Z-API) -> sempre null.
    expect(values.client_token).toBeNull();
  });
});

describe('POST /api/admin/integrations — paridade com providers vizinhos', () => {
  it('z-api SEM base_url herda o default Z-API (comportamento inalterado)', async () => {
    const { POST } = await import('@/app/api/admin/integrations/route');
    const res = await POST(
      postReq({
        agent_id: AGENT_ID,
        provider: 'z-api',
        identifier: '5511999999999',
        instance_id: 'inst-1',
        token: 'tok',
        client_token: 'ct',
        // base_url ausente -> default
      }),
    );

    expect(res.status).toBe(200);
    const values = lastInsert()!.values as Record<string, unknown>;
    expect(values.base_url).toBe(Z_API_DEFAULT_BASE_URL);
    expect(values.instance_id).toBe('inst-1');
    expect(values.client_token).toBe('ct');
  });

  it('uazapi SEM base_url retorna 400 (default Z-API não se aplica)', async () => {
    const { POST } = await import('@/app/api/admin/integrations/route');
    const res = await POST(
      postReq({
        agent_id: AGENT_ID,
        provider: 'uazapi',
        identifier: '5511999999999',
        token: 'tok',
        // base_url ausente
      }),
    );

    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.error).toBe('base_url é obrigatório para uazapi');
    expect(fake.writes).toHaveLength(0);
  });
});

describe('POST /api/admin/integrations — token de webhook no INSERT (§1.1/§4.1)', () => {
  // O INSERT de uma integração NOVA (existingByAgent === null) gera o token
  // SERVER-SIDE: o quarteto `webhook_token*` é injetado no insertPayload (FORA do
  // payload compartilhado), nunca lido do corpo. Vale para os 3 providers WhatsApp,
  // com a tag de prefixo correta por provider.
  it.each([
    ['z-api', undefined],
    ['uazapi', 'https://uaz.example.com'],
    ['evolution', 'https://evo.example.com'],
  ])('INSERT de "%s" inclui o quarteto webhook_token* gerado', async (provider, baseUrl) => {
    const { POST } = await import('@/app/api/admin/integrations/route');
    const res = await POST(
      postReq({
        agent_id: AGENT_ID,
        provider,
        identifier: '5511999999999',
        instance_id: 'inst-1',
        token: 'tok',
        ...(baseUrl ? { base_url: baseUrl } : {}),
      }),
    );

    expect(res.status).toBe(200);
    const values = lastInsert()!.values as Record<string, unknown>;

    // Os 4 campos estão presentes e não-vazios.
    for (const field of WEBHOOK_TOKEN_FIELDS) {
      expect(values[field], `INSERT deve conter ${field}`).toBeTruthy();
    }

    const token = values.webhook_token as string;
    const tag = WEBHOOK_TOKEN_TAGS[provider];
    // Formato pinado: `wh_{tag}_{base64url(32 bytes)}` (43 chars base64url).
    expect(token).toMatch(new RegExp(`^wh_${tag}_[A-Za-z0-9_-]{43}$`));
    // hash = sha256 hex (64 chars); prefix = primeiros 12 chars do token.
    expect(values.webhook_token_hash).toMatch(/^[0-9a-f]{64}$/);
    expect(values.webhook_token_prefix).toBe(token.slice(0, 12));
    // rotated_at é um ISO timestamp válido.
    expect(Number.isNaN(Date.parse(values.webhook_token_rotated_at as string))).toBe(false);
  });

  it("provider 'none' NÃO gera token (sem webhook)", async () => {
    const { POST } = await import('@/app/api/admin/integrations/route');
    const res = await POST(
      postReq({
        agent_id: AGENT_ID,
        provider: 'none',
        identifier: '5511999999999',
      }),
    );

    expect(res.status).toBe(200);
    const values = lastInsert()!.values as Record<string, unknown>;
    for (const field of WEBHOOK_TOKEN_FIELDS) {
      expect(values).not.toHaveProperty(field);
    }
  });

  it('NUNCA aceita webhook_token* vindo do corpo (gerado só server-side)', async () => {
    const { POST } = await import('@/app/api/admin/integrations/route');
    const FORGED_TOKEN = 'wh_zapi_forjado-pelo-cliente';
    const res = await POST(
      postReq({
        agent_id: AGENT_ID,
        provider: 'z-api',
        identifier: '5511999999999',
        instance_id: 'inst-1',
        token: 'tok',
        // Tentativa de injeção: o corpo NUNCA deve sobrescrever o token gerado.
        webhook_token: FORGED_TOKEN,
        webhook_token_hash: 'deadbeef',
        webhook_token_prefix: 'wh_zapi_forj',
      }),
    );

    expect(res.status).toBe(200);
    const values = lastInsert()!.values as Record<string, unknown>;
    expect(values.webhook_token).not.toBe(FORGED_TOKEN);
    expect(values.webhook_token).toMatch(/^wh_zapi_[A-Za-z0-9_-]{43}$/);
    expect(values.webhook_token_hash).not.toBe('deadbeef');
  });
});

describe('POST /api/admin/integrations — token NÃO regenerado em UPDATE benigno (§4.1)', () => {
  // Um edit benigno de uma integração que JÁ TEM token (webhook_token_hash != null)
  // NÃO pode rotacionar o token — a URL viva do cliente deve sobreviver. O
  // UPDATE compartilhado não toca em nenhum campo webhook_token*.
  it('UPDATE de linha COM hash não inclui nenhum campo webhook_token*', async () => {
    configureUpdatePath('hash-ja-existente');
    requireAdminSession.mockResolvedValue({ session: companyAdminSession() });

    const { POST } = await import('@/app/api/admin/integrations/route');
    const res = await POST(
      postReq({
        agent_id: AGENT_ID,
        provider: 'z-api',
        identifier: '5511999999999',
        instance_id: 'inst-1',
        token: 'novo-tok-outbound',
      }),
    );

    expect(res.status).toBe(200);
    // É UPDATE (não INSERT): URL viva preservada.
    expect(lastInsert()).toBeUndefined();
    const values = lastUpdate()!.values as Record<string, unknown>;
    for (const field of WEBHOOK_TOKEN_FIELDS) {
      expect(values, `UPDATE benigno NÃO pode tocar ${field}`).not.toHaveProperty(field);
    }
  });

  it('HEAL: UPDATE de linha legada SEM hash gera o quarteto webhook_token*', async () => {
    configureUpdatePath(null);
    requireAdminSession.mockResolvedValue({ session: companyAdminSession() });

    const { POST } = await import('@/app/api/admin/integrations/route');
    const res = await POST(
      postReq({
        agent_id: AGENT_ID,
        provider: 'z-api',
        identifier: '5511999999999',
        instance_id: 'inst-1',
        token: 'tok',
      }),
    );

    expect(res.status).toBe(200);
    expect(lastInsert()).toBeUndefined();
    const values = lastUpdate()!.values as Record<string, unknown>;
    for (const field of WEBHOOK_TOKEN_FIELDS) {
      expect(values[field], `HEAL deve preencher ${field}`).toBeTruthy();
    }
    expect(values.webhook_token).toMatch(/^wh_zapi_[A-Za-z0-9_-]{43}$/);
  });
});

describe('GET /api/admin/integrations — projeção explícita (§1.2/§4.1)', () => {
  // A projeção EXPLÍCITA de colunas devolve os secrets outbound (token/client_token)
  // E os novos webhook_token* (prefixo/rotated_at), sem o `webhook_token_hash` (não
  // sai na projeção da UI). O `webhook_token` em texto puro é re-exibido para montar
  // a URL, mas NUNCA é logado.
  it('devolve token, client_token e webhook_token*', async () => {
    configureGetPath({
      id: 'int-1',
      agent_id: AGENT_ID,
      company_id: COMPANY_ID,
      provider: 'z-api',
      identifier: '5511999999999',
      instance_id: 'inst-1',
      token: 'outbound-token',
      client_token: 'outbound-client-token',
      base_url: Z_API_DEFAULT_BASE_URL,
      is_active: true,
      webhook_token: 'wh_zapi_abc123',
      webhook_token_prefix: 'wh_zapi_abc1',
      webhook_token_rotated_at: '2026-06-26T00:00:00.000Z',
    });
    requireAdminSession.mockResolvedValue({ session: companyAdminSession() });

    const { GET } = await import('@/app/api/admin/integrations/route');
    const res = await GET(getReq(`http://t/api/admin/integrations?agentId=${AGENT_ID}`));

    expect(res.status).toBe(200);
    const body = await res.json();
    const integration = body.integration as Record<string, unknown>;

    // Secrets outbound: presentes (omiti-los faria um save benigno apagá-los).
    expect(integration.token).toBe('outbound-token');
    expect(integration.client_token).toBe('outbound-client-token');
    // webhook_token* da projeção explícita.
    expect(integration.webhook_token).toBe('wh_zapi_abc123');
    expect(integration.webhook_token_prefix).toBe('wh_zapi_abc1');
    expect(integration.webhook_token_rotated_at).toBe('2026-06-26T00:00:00.000Z');
  });

  it('sem integração devolve integration=null e webhook_url_unavailable', async () => {
    fake = createFakeSupabase({
      tables: {
        agents: { selectResults: [{ data: { id: AGENT_ID, company_id: COMPANY_ID } }] },
        integrations: { selectResults: [{ data: [] }] },
      },
    });
    requireAdminSession.mockResolvedValue({ session: companyAdminSession() });

    const { GET } = await import('@/app/api/admin/integrations/route');
    const res = await GET(getReq(`http://t/api/admin/integrations?agentId=${AGENT_ID}`));

    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.integration).toBeNull();
    expect(body.webhook_url_unavailable).toBe(true);
  });
});

describe('integrations — isolamento cross-tenant (§6.3/§7)', () => {
  // Um company_admin escopado em COMPANY_ID que passa um company_id de OUTRO tenant
  // recebe 404 genérico ('Recurso não encontrado') no guard resolveTargetCompanyId —
  // ANTES de qualquer leitura/escrita da linha alheia (nada vaza, nada é gravado).
  it('GET com company_id de outro tenant => 404 e nada lido/escrito', async () => {
    fake = createFakeSupabase({ tables: {} });
    requireAdminSession.mockResolvedValue({ session: companyAdminSession() });

    const { GET } = await import('@/app/api/admin/integrations/route');
    const res = await GET(
      getReq(
        `http://t/api/admin/integrations?agentId=${AGENT_ID}&company_id=${OTHER_COMPANY_ID}`,
      ),
    );

    expect(res.status).toBe(404);
    const body = await res.json();
    expect(body.error).toBe('Recurso não encontrado');
    expect(fake.writes).toHaveLength(0);
  });

  it('POST com company_id de outro tenant => 404 e SEM escrita (sem token gerado)', async () => {
    fake = createFakeSupabase({ tables: {} });
    requireAdminSession.mockResolvedValue({ session: companyAdminSession() });

    const { POST } = await import('@/app/api/admin/integrations/route');
    const res = await POST(
      postReq({
        agent_id: AGENT_ID,
        company_id: OTHER_COMPANY_ID,
        provider: 'z-api',
        identifier: '5511999999999',
        instance_id: 'inst-1',
        token: 'tok',
      }),
    );

    expect(res.status).toBe(404);
    const body = await res.json();
    expect(body.error).toBe('Recurso não encontrado');
    expect(fake.writes).toHaveLength(0);
  });
});
