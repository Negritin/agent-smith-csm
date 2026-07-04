import { NextRequest, NextResponse } from 'next/server';
import { randomBytes, createHash } from 'crypto';
import { createClient } from '@supabase/supabase-js';
import { requireAdminSession } from '@/lib/auth-actions';
import { apiError } from '@/lib/api-error';
import { errorLogFields, log } from '@/lib/logger';
import { auditMasterAdminCompanyOverride, logSecurityAudit } from '@/lib/security-audit';

export const dynamic = 'force-dynamic';

/**
 * Provedores WhatsApp aceitos no write path.
 *
 * ESTREITADO para os 3 providers realmente implementados por bridge no backend:
 * `z-api`, `uazapi`, `evolution`. Aliases órfãos antigos (`evolution-api`,
 * `wppconnect`, `whatsapp`, `whatsapp-cloud`, `meta`) NÃO são mais aceitos.
 *
 * POR QUE o estreitamento é SEGURO agora: a migração datada
 * (`20260625_01_whatsapp_provider_seam.sql`) já rodou ANTES desta release e:
 *   - NEUTRALIZOU as linhas órfãs (provider sem bridge) marcando-as
 *     `is_active = false`, de modo que nenhuma integração viva referencia um
 *     alias morto;
 *   - NORMALIZOU `evolution-api` -> `evolution`, então não sobra nenhuma linha
 *     legada com o nome antigo para ser re-salva.
 * Logo, re-salvar qualquer integração existente cai num provider ∈ {z-api,
 * uazapi, evolution} e passa pela whitelist. ⚠️ ORDEM DE DEPLOY: inverter
 * (estreitar ANTES da migração) faria um UPDATE de uma linha legada
 * `evolution-api`/órfã retornar 400 — esta release sobe DEPOIS da migração.
 *
 * É o MESMO conjunto usado:
 *   - pelo índice único parcial / dedup da migração (§2.2/§6.2);
 *   - pelos lookups de exclusividade abaixo (§6.3);
 *   - pelo badge `has_whatsapp` (§2.3, agent_service);
 *   - pelo registry de providers do backend (resolve_provider).
 *
 * ⚠️ INVARIANTE DE SINCRONIA TRIPLA: {z-api, uazapi, evolution} DEVE bater em 3
 * pontos — Python (integration_service.WHATSAPP_PROVIDERS), AQUI (route.ts) e o
 * literal SQL `provider IN (...)` da migração datada
 * `20260625_01_whatsapp_provider_seam.sql`. Drift quebra o build
 * (test_integration_route_exclusivity / test_uazapi_*).
 */
const WHATSAPP_PROVIDERS = ['z-api', 'uazapi', 'evolution'] as const;

// Whitelist de provider do POST: WHATSAPP_PROVIDERS + 'none' (§7.3). Fora => 400.
const ALLOWED_PROVIDERS = new Set<string>([...WHATSAPP_PROVIDERS, 'none']);

/**
 * Tag de observabilidade do token de webhook por provider (§1.1). Só serve para
 * grep de log/prefixo — NÃO revela entropia. PINADO no CONTRATO (idêntico ao
 * backfill Python e ao regenerate/route.ts):
 *   z-api -> zapi | uazapi -> uaz | evolution -> evo.
 */
const WEBHOOK_TOKEN_TAGS: Record<string, string> = {
  'z-api': 'zapi',
  uazapi: 'uaz',
  evolution: 'evo',
};

type WebhookTokenFields = {
  webhook_token: string;
  webhook_token_hash: string;
  webhook_token_prefix: string;
  webhook_token_rotated_at: string;
};

/**
 * Gera o quarteto de campos do token de webhook (§1.1/§4.1).
 *
 * Formato PINADO no CONTRATO: `wh_{tag}_{base64url(randomBytes(32))}` (256 bits,
 * 43 chars base64url sem padding) — idêntico ao backfill (`token_urlsafe(32)`) e
 * ao regenerate. `webhook_token_hash` = sha256(token) hex(64) é a chave de
 * lookup do inbound; `webhook_token_prefix` = primeiros 12 chars (não-secreto)
 * para UI/audit/log. O token em texto puro NUNCA é logado.
 */
function buildWebhookTokenFields(provider: string): WebhookTokenFields {
  const tag = WEBHOOK_TOKEN_TAGS[provider] ?? 'zapi';
  const token = `wh_${tag}_${randomBytes(32).toString('base64url')}`;
  return {
    webhook_token: token,
    webhook_token_hash: createHash('sha256').update(token).digest('hex'),
    webhook_token_prefix: token.slice(0, 12),
    webhook_token_rotated_at: new Date().toISOString(),
  };
}

/**
 * Monta a URL pública de webhook SERVER-SIDE a partir de `NEXT_PUBLIC_API_URL`
 * (§1.3), com guarda anti-localhost: se a var estiver ausente OU não for uma URL
 * pública `https://`, NÃO monta a URL e sinaliza `webhook_url_unavailable` — a UI
 * mostra "configure NEXT_PUBLIC_API_URL" em vez de colar um localhost que
 * quebraria todo o inbound. SEM fallback localhost (roda em prod no Railway).
 *
 * `token` ausente (linha legada ainda sem token) => também devolve flag.
 */
function buildWebhookUrl(
  provider: string,
  token: string | null | undefined,
): { webhookUrl: string | null; webhookUrlBase: string | null; webhookUrlUnavailable: boolean } {
  const rawApiUrl = process.env.NEXT_PUBLIC_API_URL?.trim();

  let base: string | null = null;
  if (rawApiUrl) {
    try {
      const parsed = new URL(rawApiUrl);
      // Só URL pública https:// é aceita. localhost/127.* e *.railway.internal
      // (interno server-to-server) NÃO servem para o cliente colar no painel.
      const host = parsed.hostname.toLowerCase();
      const isLocal =
        host === 'localhost' ||
        host === '127.0.0.1' ||
        host === '::1' ||
        host.endsWith('.localhost');
      const isInternal = host.endsWith('.railway.internal');
      if (parsed.protocol === 'https:' && !isLocal && !isInternal) {
        base = rawApiUrl.replace(/\/+$/, '');
      }
    } catch {
      base = null;
    }
  }

  if (!base || !token) {
    return { webhookUrl: null, webhookUrlBase: base, webhookUrlUnavailable: true };
  }

  return {
    webhookUrl: `${base}/api/v1/webhook/${provider}/${token}`,
    webhookUrlBase: base,
    webhookUrlUnavailable: false,
  };
}

// Service Role Client
const supabaseAdmin = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY!,
  { auth: { persistSession: false } },
);

type AdminContext = {
  companyId: string | null;
  adminId: string;
  role: 'master_admin' | 'company_admin';
  isMasterAdmin: boolean;
};

async function requireAdminContext(request: NextRequest): Promise<AdminContext | NextResponse> {
  const auth = await requireAdminSession();
  if (auth.response) {
    return apiError('Não autorizado', { request, status: auth.response.status || 401 });
  }

  if (auth.session.role === 'company_admin' && !auth.session.companyId) {
    return apiError('Contexto de empresa obrigatório', { request, status: 403 });
  }

  return {
    companyId: auth.session.companyId || null,
    adminId: auth.session.adminId,
    role: auth.session.role,
    isMasterAdmin: auth.session.role === 'master_admin',
  };
}

function readString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function readQueryCompanyId(request: NextRequest): string | null {
  return (
    readString(request.nextUrl.searchParams.get('companyId')) ||
    readString(request.nextUrl.searchParams.get('company_id'))
  );
}

function readBodyCompanyId(body: Record<string, unknown>): string | null {
  return readString(body.companyId) || readString(body.company_id);
}

async function resolveTargetCompanyId(
  request: NextRequest,
  adminContext: AdminContext,
  explicitCompanyId: string | null,
): Promise<string | NextResponse> {
  if (adminContext.isMasterAdmin) {
    if (!explicitCompanyId) {
      return apiError('Contexto de empresa obrigatório', { request, status: 400 });
    }

    await auditMasterAdminCompanyOverride({
      request,
      actorId: adminContext.adminId,
      sessionCompanyId: adminContext.companyId,
      frontendCompanyId: explicitCompanyId,
      resourceType: 'integrations',
      resourceId: explicitCompanyId,
      action: `${request.method} /api/admin/integrations`,
    });

    return explicitCompanyId;
  }

  if (!adminContext.companyId) {
    return apiError('Contexto de empresa obrigatório', { request, status: 403 });
  }

  if (explicitCompanyId && explicitCompanyId !== adminContext.companyId) {
    await logSecurityAudit({
      action: 'cross_tenant_attempt',
      actorId: adminContext.adminId,
      actorRole: adminContext.role,
      companyId: adminContext.companyId,
      targetCompanyId: explicitCompanyId,
      resourceType: 'integrations',
      resourceId: explicitCompanyId,
      request,
      status: 'error',
      details: {
        attemptedAction: `${request.method} /api/admin/integrations`,
      },
    });

    return apiError('Recurso não encontrado', { request, status: 404 });
  }

  return adminContext.companyId;
}

function isUniqueConflict(error: { code?: string; message?: string } | null): boolean {
  return error?.code === '23505' || Boolean(error?.message?.includes('duplicate key'));
}

/**
 * GET /api/admin/integrations?agentId={id}
 * Fetch integration (WhatsApp) for a specific agent
 * 🔒 SECURITY: Validates that agent belongs to user's company
 */
export async function GET(request: NextRequest) {
  try {
    const adminContext = await requireAdminContext(request);
    if (adminContext instanceof NextResponse) return adminContext;

    const { searchParams } = new URL(request.url);
    const agentId = searchParams.get('agentId');

    if (!agentId) {
      return apiError('agentId é obrigatório', { request, status: 400 });
    }

    const targetCompanyId = await resolveTargetCompanyId(
      request,
      adminContext,
      readQueryCompanyId(request),
    );
    if (targetCompanyId instanceof NextResponse) return targetCompanyId;

    // 🔒 SECURITY: First verify the agent exists in the explicit/admin tenant
    const { data: agent, error: agentError } = await supabaseAdmin
      .from('agents')
      .select('id, company_id')
      .eq('id', agentId)
      .eq('company_id', targetCompanyId)
      .single();

    if (agentError || !agent) {
      return apiError('Agente não encontrado', { request, status: 404 });
    }

    // Now safe to fetch integration.
    // §1.2/§4.1 — projeção EXPLÍCITA de colunas (NÃO select('*')): evita vazar
    // uma coluna nova por acidente no futuro. INCLUI `token`/`client_token`
    // (outbound — a UI os carrega em campos editáveis e os re-envia no save;
    // omiti-los faria um save benigno sobrescrevê-los com vazio) E os novos
    // `webhook_token*`. O `webhook_token` (texto puro) NUNCA é logado.
    const { data, error } = await supabaseAdmin
      .from('integrations')
      // eslint-disable-next-line prettier/prettier -- string literal único: o parser de tipos do supabase-js exige literal (não concatenado) para inferir as colunas
      .select('id, agent_id, company_id, provider, identifier, instance_id, token, client_token, base_url, is_active, buffer_enabled, buffer_debounce_seconds, buffer_max_wait_seconds, webhook_token, webhook_token_prefix, webhook_token_rotated_at, created_at, updated_at')
      .eq('agent_id', agentId)
      .eq('company_id', targetCompanyId)
      .order('created_at', { ascending: false })
      .limit(1);

    if (error) {
      log.error('[INTEGRATIONS API] Query failed', { errorCode: error.code });
      return apiError('Erro ao carregar integração', { request, status: 500 });
    }

    const integration = data && data.length > 0 ? data[0] : null;

    // URL de webhook montada SERVER-SIDE de NEXT_PUBLIC_API_URL (§1.3), com
    // guarda anti-localhost. A UI consome { webhookUrl, webhookUrlBase,
    // webhook_url_unavailable } — nunca recomputa no cliente com fallback local.
    const webhookInfo = integration
      ? buildWebhookUrl(integration.provider, integration.webhook_token)
      : { webhookUrl: null, webhookUrlBase: null, webhookUrlUnavailable: true };

    return NextResponse.json({
      integration,
      webhookUrl: webhookInfo.webhookUrl,
      webhookUrlBase: webhookInfo.webhookUrlBase,
      webhook_url_unavailable: webhookInfo.webhookUrlUnavailable,
    });
  } catch (error: unknown) {
    log.error('[INTEGRATIONS API] Error', errorLogFields(error));
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[INTEGRATIONS API] GET failed',
      request,
      status: 500,
    });
  }
}

/**
 * POST /api/admin/integrations
 * Create or update integration in the resolved tenant scope.
 * 🔒 SECURITY: Master admin must provide companyId; company admin uses session companyId.
 */
export async function POST(request: NextRequest) {
  try {
    const adminContext = await requireAdminContext(request);
    if (adminContext instanceof NextResponse) return adminContext;

    const body = await request.json().catch(() => null);
    if (!body || typeof body !== 'object' || Array.isArray(body)) {
      return apiError('Requisição inválida', { request, status: 400 });
    }
    const bodyRecord = body as Record<string, unknown>;
    const {
      agent_id,
      provider,
      identifier,
      instance_id,
      token,
      client_token,
      base_url,
      is_active,
      buffer_enabled,
      buffer_debounce_seconds,
      buffer_max_wait_seconds,
    } = bodyRecord;

    const agentId = readString(agent_id);
    if (!agentId) {
      return apiError('agent_id é obrigatório', { request, status: 400 });
    }

    const targetCompanyId = await resolveTargetCompanyId(
      request,
      adminContext,
      readBodyCompanyId(bodyRecord) || readQueryCompanyId(request),
    );
    if (targetCompanyId instanceof NextResponse) return targetCompanyId;

    // 🔒 SECURITY: Verify the agent exists in the explicit/admin tenant
    const { data: agent, error: agentError } = await supabaseAdmin
      .from('agents')
      .select('id, company_id')
      .eq('id', agentId)
      .eq('company_id', targetCompanyId)
      .single();

    if (agentError || !agent) {
      return apiError('Agente não encontrado', { request, status: 404 });
    }

    const integrationProvider = readString(provider) || 'z-api';

    // Whitelist alinhada ao backend WHATSAPP_PROVIDERS + 'none' (§7.3). Fora => 400.
    // Estreitada: só os providers REALMENTE implementados por bridge passam
    // ('z-api'/'uazapi'/'evolution'). Aliases órfãos (wppconnect/meta/whatsapp/
    // whatsapp-cloud) e o alias não-normalizado 'evolution-api' caem como 400.
    if (!ALLOWED_PROVIDERS.has(integrationProvider)) {
      return apiError('Provider inválido', { request, status: 400 });
    }

    const integrationIdentifier = typeof identifier === 'string' ? identifier.trim() : '';
    const trimmedInstanceId = typeof instance_id === 'string' ? instance_id.trim() : '';
    const isUazapi = integrationProvider === 'uazapi';
    const isEvolution = integrationProvider === 'evolution';

    // base_url: default z-api SÓ para o próprio z-api. uazapi e evolution apontam
    // para servidores próprios (host self-hosted/cluster), então NÃO herdam o
    // default https://api.z-api.io/instances — começam em '' e exigem base_url.
    const resolvedBaseUrl =
      typeof base_url === 'string' && base_url.trim()
        ? base_url.trim()
        : isUazapi || isEvolution
          ? ''
          : 'https://api.z-api.io/instances';

    // uazapi: base_url OBRIGATÓRIO (§7.3).
    if (isUazapi && !resolvedBaseUrl) {
      return apiError('base_url é obrigatório para uazapi', { request, status: 400 });
    }

    // evolution: base_url (servidor Evolution) E instance_id OBRIGATÓRIOS, SEM
    // default Z-API. Espelha o tratamento do uazapi (apiError 400 em português).
    if (isEvolution) {
      if (!resolvedBaseUrl) {
        return apiError('base_url é obrigatório para evolution', { request, status: 400 });
      }
      if (!trimmedInstanceId) {
        return apiError('instance_id é obrigatório para evolution', { request, status: 400 });
      }
    }

    const payload = {
      agent_id: agentId,
      company_id: targetCompanyId,
      provider: integrationProvider,
      // evolution: identifier = connectedPhone (número conectado na instância).
      identifier: integrationIdentifier,
      // uazapi não usa instance_id -> null (requer migração §2.2.1 DROP NOT NULL);
      // evolution e z-api persistem o instance_id informado (evolution: obrigatório).
      instance_id: isUazapi ? null : trimmedInstanceId,
      // evolution: token = apikey da instância.
      token: typeof token === 'string' ? token.trim() : '',
      // evolution não usa client_token (conceito Z-API) -> sempre null.
      client_token: isEvolution
        ? null
        : typeof client_token === 'string'
          ? client_token.trim()
          : null,
      base_url: resolvedBaseUrl,
      is_active: is_active ?? true,
      buffer_enabled: buffer_enabled ?? true,
      buffer_debounce_seconds: buffer_debounce_seconds ?? 3,
      buffer_max_wait_seconds: buffer_max_wait_seconds ?? 10,
      updated_at: new Date().toISOString(),
    };

    // §6.3 Passo 1 — Guard de conflito cross-tenant PROVIDER-AGNÓSTICO.
    // Consulta por identifier em TODOS os providers WhatsApp e retorna LISTA
    // (nunca .maybeSingle(), que lançaria com >1 linha). Fecha o buraco em que a
    // empresa B reivindicaria via uazapi um número já registrado como z-api da
    // empresa A (par do §3.7 no lado de escrita).
    const { data: identifierRows, error: identifierError } = await supabaseAdmin
      .from('integrations')
      .select('id, company_id, provider, agent_id')
      .eq('identifier', integrationIdentifier)
      .in('provider', WHATSAPP_PROVIDERS as unknown as string[]);

    if (identifierError) {
      log.error('[INTEGRATIONS API] Lookup failed', { errorCode: identifierError.code });
      return apiError('Erro ao salvar integração', { request, status: 500 });
    }

    if ((identifierRows ?? []).some((row) => row.company_id !== targetCompanyId)) {
      log.warn('[INTEGRATIONS API] Cross-tenant integration identifier conflict', {
        targetCompanyId,
      });
      return apiError('Número já em uso por outra empresa', { request, status: 409 });
    }

    // §6.3 Passo 2 — Lookup `existingByAgent` tolerante a duplicatas (dirty-data).
    // SEM .maybeSingle() (lança com >1 linha). Ordena is_active DESC (prioriza a
    // linha ATIVA — a única restringida pelo índice parcial), depois updated_at e
    // created_at DESC, e lê data?.[0]. Espelha o padrão defensivo do GET handler.
    const { data: agentRows, error: agentRowsError } = await supabaseAdmin
      .from('integrations')
      .select('id, provider, identifier, company_id, is_active, webhook_token_hash')
      .eq('agent_id', agentId)
      .eq('company_id', targetCompanyId)
      .in('provider', WHATSAPP_PROVIDERS as unknown as string[])
      .order('is_active', { ascending: false })
      .order('updated_at', { ascending: false })
      .order('created_at', { ascending: false })
      .limit(1);

    if (agentRowsError) {
      log.error('[INTEGRATIONS API] Lookup failed', { errorCode: agentRowsError.code });
      return apiError('Erro ao salvar integração', { request, status: 500 });
    }

    const existingByAgent = agentRows?.[0] ?? null;

    // §1.1/§4.1 — Token de webhook gerado SERVER-SIDE (nunca do corpo). Os 4
    // campos ficam FORA do `payload` compartilhado:
    //   - INSERT (existingByAgent === null): gerar e injetar no insert (token novo).
    //   - UPDATE: gerar SÓ como HEAL de linha legada/reativada sem token
    //     (webhook_token_hash IS NULL). NUNCA rotacionar um token já existente —
    //     um edit benigno não pode mudar a URL viva do cliente (rotação é só via
    //     /api/admin/integrations/regenerate).
    // Provider 'none' não tem webhook; só geramos para os providers WhatsApp.
    const isWhatsappProvider = (WHATSAPP_PROVIDERS as readonly string[]).includes(
      integrationProvider,
    );

    // §6.3 Passo 3 — Resolução determinística (XOR garantido):
    //   existingByAgent existe  => SEMPRE UPDATE in-place (mesmo id). NUNCA INSERT.
    //                              Cobre os 3 casos de troca (mesmo provider/número,
    //                              troca de provider mesmo identifier, troca com
    //                              identifier novo) colapsando numa única linha.
    //   existingByAgent é null   => INSERT.
    let writeResult;
    if (existingByAgent) {
      // HEAL: gerar token só quando a linha ainda não tem hash. Caso contrário,
      // o UPDATE compartilhado NÃO toca em nenhum campo webhook_token* (URL viva
      // preservada).
      const healFields =
        isWhatsappProvider && !existingByAgent.webhook_token_hash
          ? buildWebhookTokenFields(integrationProvider)
          : null;
      const updatePayload = healFields ? { ...payload, ...healFields } : payload;
      writeResult = await supabaseAdmin
        .from('integrations')
        .update(updatePayload)
        .eq('id', existingByAgent.id)
        .eq('company_id', targetCompanyId)
        .select()
        .single();
    } else {
      // INSERT: objeto insert-only com os 4 campos de token (fora do payload
      // compartilhado), só para providers WhatsApp.
      const insertPayload = isWhatsappProvider
        ? { ...payload, ...buildWebhookTokenFields(integrationProvider) }
        : payload;
      writeResult = await supabaseAdmin
        .from('integrations')
        .insert(insertPayload)
        .select()
        .single();
    }

    if (writeResult.error) {
      if (isUniqueConflict(writeResult.error)) {
        return apiError('Identificador de integração já está em uso', {
          request,
          status: 409,
        });
      }
      log.error('[INTEGRATIONS API] Save failed', { errorCode: writeResult.error.code });
      return apiError('Erro ao salvar integração', { request, status: 500 });
    }

    // URL de webhook montada SERVER-SIDE (§1.3) com guarda anti-localhost. O
    // token vem da linha persistida (write devolve a coluna webhook_token).
    const savedIntegration = writeResult.data;
    const webhookInfo = buildWebhookUrl(savedIntegration.provider, savedIntegration.webhook_token);

    return NextResponse.json({
      integration: savedIntegration,
      webhookUrl: webhookInfo.webhookUrl,
      webhookUrlBase: webhookInfo.webhookUrlBase,
      webhook_url_unavailable: webhookInfo.webhookUrlUnavailable,
    });
  } catch (error: unknown) {
    log.error('[INTEGRATIONS API] Error', errorLogFields(error));
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[INTEGRATIONS API] POST failed',
      request,
      status: 500,
    });
  }
}

/**
 * DELETE /api/admin/integrations?id={integrationId}
 * Delete an integration
 * 🔒 SECURITY: Validates integration belongs to user's company
 */
export async function DELETE(request: NextRequest) {
  try {
    const adminContext = await requireAdminContext(request);
    if (adminContext instanceof NextResponse) return adminContext;

    const { searchParams } = new URL(request.url);
    const id = searchParams.get('id');

    if (!id) {
      return apiError('id é obrigatório', { request, status: 400 });
    }

    const targetCompanyId = await resolveTargetCompanyId(
      request,
      adminContext,
      readQueryCompanyId(request),
    );
    if (targetCompanyId instanceof NextResponse) return targetCompanyId;

    const { data: integration, error: loadError } = await supabaseAdmin
      .from('integrations')
      .select('id, company_id')
      .eq('id', id)
      .single();

    if (loadError || !integration) {
      return apiError('Integração não encontrada', { request, status: 404 });
    }

    if (integration.company_id !== targetCompanyId) {
      log.warn('[INTEGRATIONS API] Unauthorized delete attempt', {
        targetCompanyId,
        integrationCompanyId: integration.company_id,
      });
      await logSecurityAudit({
        action: 'cross_tenant_attempt',
        actorId: adminContext.adminId,
        actorRole: adminContext.role,
        companyId: adminContext.companyId || targetCompanyId,
        targetCompanyId: integration.company_id,
        resourceType: 'integrations',
        resourceId: id,
        request,
        status: 'error',
        details: {
          attemptedAction: 'delete_integration',
          requestedCompanyId: targetCompanyId,
        },
      });

      return apiError('Integração não encontrada', { request, status: 404 });
    }

    const { error } = await supabaseAdmin
      .from('integrations')
      .delete()
      .eq('company_id', targetCompanyId)
      .eq('id', id);

    if (error) {
      log.error('[INTEGRATIONS API] Delete failed', { errorCode: error.code });
      return apiError('Erro ao remover integração', { request, status: 500 });
    }

    return NextResponse.json({ success: true });
  } catch (error: unknown) {
    log.error('[INTEGRATIONS API] Error', errorLogFields(error));
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[INTEGRATIONS API] DELETE failed',
      request,
      status: 500,
    });
  }
}
