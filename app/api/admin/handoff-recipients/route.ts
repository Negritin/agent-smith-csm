import { NextRequest, NextResponse } from 'next/server';
import { apiError, authApiError } from '@/lib/api-error';
import { requireAdminSession } from '@/lib/auth-actions';
import { log } from '@/lib/logger';
import { normalizePhone } from '@/lib/normalize-phone';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

export const dynamic = 'force-dynamic';

const supabaseAdmin = getSupabaseAdmin();

/**
 * GET/POST /api/admin/handoff-recipients (§9.4).
 *
 * GET: lista destinatários de alerta de handoff da empresa (opcionalmente por
 * agent_id). POST: cria destinatário; para WhatsApp normaliza o telefone e
 * cria/reativa a blocklist interna derivada (§8.4) para que o número do operador
 * não dispare a IA quando ele responder ao alerta.
 */

const RECIPIENT_COLUMNS =
  'id, company_id, agent_id, channel, recipient_value, recipient_normalized, ' +
  'display_name, enabled, created_at, updated_at';

async function resolveCompanyId(
  request: Request,
): Promise<
  { companyId: string; creatorUserId: string | null; response?: never } | { response: NextResponse }
> {
  const result = await requireAdminSession();
  if (result.response) {
    return { response: await authApiError(result.response, { request }) };
  }
  const session = result.session;
  if (session.role === 'company_admin') {
    if (!session.companyId) {
      return { response: apiError('Não autorizado', { request, status: 403 }) };
    }
    // company_admin.adminId É uma linha de users_v2 → seguro como created_by.
    return { companyId: session.companyId, creatorUserId: session.adminId };
  }
  const url = new URL(request.url);
  const queryCompanyId = url.searchParams.get('company_id') || url.searchParams.get('companyId');
  if (!queryCompanyId) {
    return {
      response: apiError('company_id é obrigatório para master_admin', { request, status: 400 }),
    };
  }
  // master_admin.adminId vem de admin_users (SEM relação com users_v2). A coluna
  // handoff_notification_recipients.created_by faz FK para users_v2(id); passar o id
  // do master_admin causaria foreign_key_violation (23503). null evita o 500.
  return { companyId: queryCompanyId, creatorUserId: null };
}

export async function GET(request: NextRequest) {
  try {
    const resolved = await resolveCompanyId(request);
    if (resolved.response) return resolved.response;

    const url = new URL(request.url);
    const agentId = url.searchParams.get('agent_id') || url.searchParams.get('agentId');

    let query = supabaseAdmin
      .from('handoff_notification_recipients')
      .select(RECIPIENT_COLUMNS)
      .eq('company_id', resolved.companyId)
      .order('created_at', { ascending: false });

    if (agentId) query = query.eq('agent_id', agentId);

    const { data, error } = await query;
    if (error) {
      return apiError('Erro ao listar destinatários', {
        cause: error,
        logMessage: '[HANDOFF RECIPIENTS] select failed',
        request,
        status: 500,
      });
    }
    return NextResponse.json({ recipients: data ?? [] });
  } catch (error: unknown) {
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[HANDOFF RECIPIENTS] GET error',
      request,
      status: 500,
    });
  }
}

export async function POST(request: NextRequest) {
  try {
    const resolved = await resolveCompanyId(request);
    if (resolved.response) return resolved.response;
    const { companyId, creatorUserId } = resolved;

    const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
    const channel = body.channel;
    const recipientValue = typeof body.recipient_value === 'string' ? body.recipient_value : '';
    const displayName = typeof body.display_name === 'string' ? body.display_name : null;
    const agentId = typeof body.agent_id === 'string' ? body.agent_id : null;

    if (channel !== 'email' && channel !== 'whatsapp') {
      return apiError("channel inválido (use 'email' ou 'whatsapp')", { request, status: 400 });
    }
    if (!recipientValue) {
      return apiError('recipient_value é obrigatório', { request, status: 400 });
    }

    let recipientNormalized: string;
    if (channel === 'whatsapp') {
      const normalized = normalizePhone(recipientValue);
      if (!normalized) {
        return apiError('Telefone inválido', { request, status: 400 });
      }
      recipientNormalized = normalized;
    } else {
      recipientNormalized = recipientValue.trim().toLowerCase();
    }

    // Reativa um destinatário previamente desativado (mesma chave de unicidade
    // parcial) em vez de violar uq_handoff_recipient_active.
    let existingQuery = supabaseAdmin
      .from('handoff_notification_recipients')
      .select('id, enabled')
      .eq('company_id', companyId)
      .eq('channel', channel)
      .eq('recipient_normalized', recipientNormalized);
    existingQuery = agentId
      ? existingQuery.eq('agent_id', agentId)
      : existingQuery.is('agent_id', null);
    const { data: existing } = await existingQuery.maybeSingle();

    let recipient: Record<string, any>;
    if (existing) {
      const { data: updated, error: updateError } = await supabaseAdmin
        .from('handoff_notification_recipients')
        .update({
          enabled: true,
          recipient_value: recipientValue,
          display_name: displayName,
          updated_at: new Date().toISOString(),
        })
        .eq('id', existing.id)
        .select(RECIPIENT_COLUMNS)
        .single();
      if (updateError) {
        return apiError('Erro ao reativar destinatário', {
          cause: updateError,
          request,
          status: 500,
        });
      }
      recipient = updated;
    } else {
      const { data: created, error: insertError } = await supabaseAdmin
        .from('handoff_notification_recipients')
        .insert({
          company_id: companyId,
          agent_id: agentId,
          channel,
          recipient_value: recipientValue,
          recipient_normalized: recipientNormalized,
          display_name: displayName,
          enabled: true,
          created_by: creatorUserId,
        })
        .select(RECIPIENT_COLUMNS)
        .single();
      if (insertError) {
        return apiError('Erro ao criar destinatário', {
          cause: insertError,
          logMessage: '[HANDOFF RECIPIENTS] insert failed',
          request,
          status: 500,
        });
      }
      recipient = created;
    }

    // §9.4: ao criar destinatário WhatsApp, cria/reativa a blocklist derivada.
    if (channel === 'whatsapp') {
      try {
        let blockQuery = supabaseAdmin
          .from('internal_whatsapp_blocklist')
          .select('id')
          .eq('company_id', companyId)
          .eq('phone_normalized', recipientNormalized);
        blockQuery = agentId ? blockQuery.eq('agent_id', agentId) : blockQuery.is('agent_id', null);
        const { data: blockExisting } = await blockQuery.maybeSingle();

        if (blockExisting) {
          await supabaseAdmin
            .from('internal_whatsapp_blocklist')
            .update({ active: true, source_recipient_id: recipient.id })
            .eq('id', blockExisting.id);
        } else {
          await supabaseAdmin.from('internal_whatsapp_blocklist').insert({
            company_id: companyId,
            agent_id: agentId,
            phone_normalized: recipientNormalized,
            source_recipient_id: recipient.id,
            reason: 'handoff_notification_recipient',
            active: true,
          });
        }
      } catch (blockError: unknown) {
        log.warn('[HANDOFF RECIPIENTS] blocklist sync failed', {
          recipientId: recipient.id,
        });
      }
    }

    return NextResponse.json({ recipient });
  } catch (error: unknown) {
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[HANDOFF RECIPIENTS] POST error',
      request,
      status: 500,
    });
  }
}
