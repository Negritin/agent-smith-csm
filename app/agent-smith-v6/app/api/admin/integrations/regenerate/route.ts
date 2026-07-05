import { NextRequest, NextResponse } from 'next/server';
import { randomBytes, createHash } from 'crypto';
import { createClient } from '@supabase/supabase-js';
import { requireAdminSession } from '@/lib/auth-actions';
import { apiError } from '@/lib/api-error';
import { errorLogFields, log } from '@/lib/logger';
import { auditMasterAdminCompanyOverride, logSecurityAudit } from '@/lib/security-audit';

export const dynamic = 'force-dynamic';

/**
 * Provedores WhatsApp aceitos. MESMO conjunto (sincronia tripla) usado em
 * `route.ts` (write path), `integration_service.WHATSAPP_PROVIDERS` no backend e
 * o literal SQL `provider IN (...)` da seam migration. Aqui serve para:
 *   - mapear o provider -> tag do token (`zapi`/`uaz`/`evo`/`meta`) e ao segmento da URL;
 *   - garantir que só regeneramos token de uma integração WhatsApp.
 */
const WHATSAPP_PROVIDERS = ['z-api', 'uazapi', 'evolution', 'meta-cloud'] as const;
type WhatsAppProvider = (typeof WHATSAPP_PROVIDERS)[number];

/**
 * Tag do token por provider (CONTRATO PINADO — idêntica ao write path
 * `route.ts` e ao backfill Python). O token completo é
 * `wh_{tag}_{base64url(randomBytes(32))}`; a tag serve só para observabilidade/
 * grep de log e NÃO revela entropia.
 */
const PROVIDER_TOKEN_TAG: Record<WhatsAppProvider, string> = {
  'z-api': 'zapi',
  uazapi: 'uaz',
  evolution: 'evo',
  'meta-cloud': 'meta',
};

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

/**
 * Reusa EXATAMENTE o padrão de `route.ts:requireAdminContext`. Mantido local
 * porque o helper de `route.ts` não é exportado (mesma sessão admin, mesma
 * resposta 401/403).
 */
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

/**
 * Reusa EXATAMENTE o padrão de `route.ts:resolveTargetCompanyId`, incluindo o
 * guard 404 cross-tenant (company_admin que pede company alheia -> audita
 * `cross_tenant_attempt` e devolve 404 genérico).
 */
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
      action: `${request.method} /api/admin/integrations/regenerate`,
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
        attemptedAction: `${request.method} /api/admin/integrations/regenerate`,
      },
    });

    return apiError('Recurso não encontrado', { request, status: 404 });
  }

  return adminContext.companyId;
}

/**
 * Monta a URL de webhook SERVER-SIDE a partir de `NEXT_PUBLIC_API_URL`
 * (canônico do front). Guarda anti-localhost (§1.3): se a var estiver ausente
 * OU não for `https://` público, NÃO monta a URL — devolve flag
 * `webhookUrlUnavailable` e a UI mostra "configure NEXT_PUBLIC_API_URL".
 * SEM fallback localhost (colar `http://localhost:8000/...` no painel do
 * provider quebraria todo o inbound do cliente).
 *
 * CONTRATO DE RESPOSTA (espelha EXATAMENTE `route.ts:buildWebhookUrl` do GET):
 * devolve `{ webhookUrl, webhookUrlBase, webhookUrlUnavailable }`. A UI
 * (`WhatsAppSection`) reconstrói a URL a partir de `webhookUrlBase` + token, então
 * a resposta do regenerate PRECISA carregar `webhookUrlBase` — sem ele, o
 * `handleRegenerateWebhookToken` zeraria a base e a URL sumiria logo após um
 * regenerate bem-sucedido (cutover duro, §3.5).
 */
function buildWebhookUrl(
  provider: WhatsAppProvider,
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

/**
 * Gera o token de webhook no formato PINADO `wh_{tag}_{base64url(randomBytes(32))}`
 * (idêntico ao write path `route.ts` e ao backfill Python). Devolve os 4 campos
 * `webhook_token*` para o UPDATE. O token cru NUNCA é logado.
 */
function generateWebhookToken(provider: WhatsAppProvider) {
  const tag = PROVIDER_TOKEN_TAG[provider];
  const token = `wh_${tag}_${randomBytes(32).toString('base64url')}`;
  const webhook_token_hash = createHash('sha256').update(token).digest('hex');
  const webhook_token_prefix = token.slice(0, 12);
  return {
    token,
    fields: {
      webhook_token: token,
      webhook_token_hash,
      webhook_token_prefix,
      webhook_token_rotated_at: new Date().toISOString(),
    },
  };
}

/**
 * POST /api/admin/integrations/regenerate
 * Regenera o token de webhook de UMA integração (cutover duro D5: a URL antiga
 * para de funcionar imediatamente; a UI avisa "re-cole agora").
 * 🔒 SECURITY: master admin precisa de companyId; company admin usa a sessão.
 * O token cru só sai UMA vez na resposta — nunca em log/audit (só o prefixo).
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

    const integrationId = readString(bodyRecord.id) || readString(bodyRecord.integrationId);
    if (!integrationId) {
      return apiError('id é obrigatório', { request, status: 400 });
    }

    const targetCompanyId = await resolveTargetCompanyId(
      request,
      adminContext,
      readBodyCompanyId(bodyRecord) || readQueryCompanyId(request),
    );
    if (targetCompanyId instanceof NextResponse) return targetCompanyId;

    // Carrega a integração para validar ownership (guard cross-tenant) e obter o
    // provider — necessário para a tag do token e o segmento da URL.
    const { data: integration, error: loadError } = await supabaseAdmin
      .from('integrations')
      .select('id, company_id, provider')
      .eq('id', integrationId)
      .single();

    if (loadError || !integration) {
      return apiError('Integração não encontrada', { request, status: 404 });
    }

    // Guard 404 cross-tenant: integração de outra company -> 404 genérico
    // (mesmo padrão do DELETE em `route.ts`, sem oráculo de existência).
    if (integration.company_id !== targetCompanyId) {
      log.warn('[INTEGRATIONS REGENERATE] Cross-tenant regenerate attempt', {
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
        resourceId: integrationId,
        request,
        status: 'error',
        details: {
          attemptedAction: 'regenerate_webhook_token',
          requestedCompanyId: targetCompanyId,
        },
      });

      return apiError('Integração não encontrada', { request, status: 404 });
    }

    if (!WHATSAPP_PROVIDERS.includes(integration.provider as WhatsAppProvider)) {
      return apiError('Provider inválido para webhook', { request, status: 400 });
    }
    const provider = integration.provider as WhatsAppProvider;

    const { token, fields } = generateWebhookToken(provider);

    // UPDATE dos 4 campos webhook_token* WHERE id=? AND company_id=target.
    const { data: updated, error: updateError } = await supabaseAdmin
      .from('integrations')
      .update(fields)
      .eq('id', integrationId)
      .eq('company_id', targetCompanyId)
      .select('id, provider, webhook_token_prefix, webhook_token_rotated_at')
      .single();

    if (updateError || !updated) {
      log.error('[INTEGRATIONS REGENERATE] Update failed', {
        errorCode: updateError?.code,
      });
      return apiError('Erro ao regenerar token de webhook', { request, status: 500 });
    }

    // Auditoria: SÓ o prefixo (não-secreto). NUNCA o token cru em `details`.
    await logSecurityAudit({
      action: 'webhook_token_regenerated',
      actorId: adminContext.adminId,
      actorRole: adminContext.role,
      companyId: targetCompanyId,
      targetCompanyId,
      resourceType: 'integrations',
      resourceId: integrationId,
      request,
      status: 'success',
      details: {
        provider,
        webhookTokenPrefix: fields.webhook_token_prefix,
      },
    });

    // Monta a URL server-side (guarda anti-localhost). Sem URL pública https://
    // -> flag para a UI não exibir/copiar uma URL quebrada.
    const webhookInfo = buildWebhookUrl(provider, token);

    // Token + URL saem UMA vez na resposta (cutover duro). Nunca logados.
    // CONTRATO: devolve `webhookUrlBase` (mesma chave do GET em `route.ts`) — a
    // UI reconstrói a URL a partir da base + token. `webhookUrl` e
    // `webhook_url_unavailable` permanecem por compatibilidade.
    return NextResponse.json({
      integration: updated,
      webhook_token: token,
      webhookUrl: webhookInfo.webhookUrl,
      webhookUrlBase: webhookInfo.webhookUrlBase,
      webhook_url_unavailable: webhookInfo.webhookUrlUnavailable,
    });
  } catch (error: unknown) {
    log.error('[INTEGRATIONS REGENERATE] Error', errorLogFields(error));
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[INTEGRATIONS REGENERATE] POST failed',
      request,
      status: 500,
    });
  }
}
