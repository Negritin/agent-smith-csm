import { NextRequest, NextResponse } from 'next/server';
import { apiError, authApiError } from '@/lib/api-error';
import {
  type AdminSession,
  auditCrossTenantAttempt,
  requireAdminSession,
} from '@/lib/auth-actions';
import { log } from '@/lib/logger';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

export const dynamic = 'force-dynamic';

const supabaseAdmin = getSupabaseAdmin();

const RECIPIENT_COLUMNS =
  'id, company_id, agent_id, channel, recipient_value, recipient_normalized, ' +
  'display_name, enabled, created_at, updated_at';

async function resolveRecipient(
  request: Request,
  recipientId: string,
): Promise<
  | {
      recipient: {
        id: string;
        company_id: string;
        agent_id: string | null;
        channel: string;
        recipient_normalized: string;
      };
      session: AdminSession;
      response?: never;
    }
  | { response: NextResponse; recipient?: never; session?: never }
> {
  const result = await requireAdminSession();
  if (result.response) {
    return { response: await authApiError(result.response, { request }) };
  }
  const session = result.session;

  const { data, error } = await supabaseAdmin
    .from('handoff_notification_recipients')
    .select('id, company_id, agent_id, channel, recipient_normalized')
    .eq('id', recipientId)
    .single();

  if (error || !data) {
    return { response: apiError('Destinatário não encontrado', { request, status: 404 }) };
  }

  if (session.role === 'company_admin' && data.company_id !== session.companyId) {
    await auditCrossTenantAttempt({
      actorId: session.adminId,
      actorRole: session.role,
      actorCompanyId: session.companyId,
      resourceType: 'handoff_notification_recipients',
      resourceId: recipientId,
      targetCompanyId: data.company_id,
      action: 'modify_handoff_recipient',
      request,
    });
    return { response: apiError('Destinatário não encontrado', { request, status: 404 }) };
  }

  return {
    recipient: data as {
      id: string;
      company_id: string;
      agent_id: string | null;
      channel: string;
      recipient_normalized: string;
    },
    session,
  };
}

export async function PATCH(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  try {
    const { id } = await params;
    const resolved = await resolveRecipient(request, id);
    if (resolved.response) return resolved.response;

    const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
    const fields: Record<string, unknown> = {};
    if (typeof body.display_name === 'string' || body.display_name === null) {
      fields.display_name = body.display_name;
    }
    if (typeof body.enabled === 'boolean') fields.enabled = body.enabled;

    if (Object.keys(fields).length === 0) {
      return apiError('Nenhum campo editável informado', { request, status: 400 });
    }
    fields.updated_at = new Date().toISOString();

    const { data, error } = await supabaseAdmin
      .from('handoff_notification_recipients')
      .update(fields)
      .eq('id', id)
      .eq('company_id', resolved.recipient.company_id)
      .select(RECIPIENT_COLUMNS)
      .single();

    if (error) {
      return apiError('Erro ao atualizar destinatário', {
        cause: error,
        logMessage: '[HANDOFF RECIPIENT] update failed',
        request,
        status: 500,
      });
    }

    // Se foi DESABILITADO, sincroniza blocklist como no DELETE.
    if (fields.enabled === false) {
      await syncBlocklistOnDisable(resolved.recipient);
    }

    return NextResponse.json({ recipient: data });
  } catch (error: unknown) {
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[HANDOFF RECIPIENT] PATCH error',
      request,
      status: 500,
    });
  }
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const { id } = await params;
    const resolved = await resolveRecipient(request, id);
    if (resolved.response) return resolved.response;

    // §9.4: desativar recipient (soft) em vez de DELETE físico para preservar
    // auditoria/idempotência das deliveries que apontam para ele.
    const { error } = await supabaseAdmin
      .from('handoff_notification_recipients')
      .update({ enabled: false, updated_at: new Date().toISOString() })
      .eq('id', id)
      .eq('company_id', resolved.recipient.company_id);

    if (error) {
      return apiError('Erro ao remover destinatário', {
        cause: error,
        logMessage: '[HANDOFF RECIPIENT] delete failed',
        request,
        status: 500,
      });
    }

    await syncBlocklistOnDisable(resolved.recipient);

    return NextResponse.json({ success: true });
  } catch (error: unknown) {
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[HANDOFF RECIPIENT] DELETE error',
      request,
      status: 500,
    });
  }
}

/**
 * §9.4: ao desativar/remover um recipient WhatsApp, desativa a blocklist derivada
 * SE nenhum outro recipient ATIVO usa o mesmo recipient_normalized (mesmo escopo
 * de agent_id). Outros canais (email) não têm blocklist.
 */
async function syncBlocklistOnDisable(recipient: {
  company_id: string;
  agent_id: string | null;
  channel: string;
  recipient_normalized: string;
}): Promise<void> {
  if (recipient.channel !== 'whatsapp') return;
  try {
    let othersQuery = supabaseAdmin
      .from('handoff_notification_recipients')
      .select('id')
      .eq('company_id', recipient.company_id)
      .eq('channel', 'whatsapp')
      .eq('recipient_normalized', recipient.recipient_normalized)
      .eq('enabled', true);
    othersQuery = recipient.agent_id
      ? othersQuery.eq('agent_id', recipient.agent_id)
      : othersQuery.is('agent_id', null);
    const { data: others } = await othersQuery.limit(1);

    if (others && others.length > 0) {
      // Ainda há outro recipient ativo com o mesmo número: manter blocklist ativa.
      return;
    }

    let blockQuery = supabaseAdmin
      .from('internal_whatsapp_blocklist')
      .update({ active: false })
      .eq('company_id', recipient.company_id)
      .eq('phone_normalized', recipient.recipient_normalized)
      .eq('reason', 'handoff_notification_recipient');
    blockQuery = recipient.agent_id
      ? blockQuery.eq('agent_id', recipient.agent_id)
      : blockQuery.is('agent_id', null);
    await blockQuery;
  } catch (error: unknown) {
    log.warn('[HANDOFF RECIPIENT] blocklist disable sync failed', {
      recipientNormalized: recipient.recipient_normalized,
    });
  }
}
