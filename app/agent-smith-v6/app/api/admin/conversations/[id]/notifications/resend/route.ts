import { NextRequest, NextResponse } from 'next/server';
import { apiError } from '@/lib/api-error';
import { requireAttendanceAdmin, resolveConversation } from '@/lib/attendance-actions';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

export const dynamic = 'force-dynamic';

const supabaseAdmin = getSupabaseAdmin();

/**
 * Event_types de ALERTA de handoff que o worker de outbox sabe (re)despachar
 * (§11.1/§11.4). DEVE bater com `_ALERT_EVENT_TYPES` no NotificationService.
 * `human_message` é AUDITORIA da mensagem humana ao cliente e NUNCA pode ser
 * reenfileirada: o worker a despacharia como alerta interno de handoff (com URL
 * admin) para o telefone do CLIENTE.
 */
const ALERT_EVENT_TYPES = ['handoff_requested', 'handoff_notified', 'test_notification'];

/**
 * POST /api/admin/conversations/[id]/notifications/resend (§9.1, §8.3)
 *
 * Reenfileira as entregas de notificação `failed`/`skipped` da conversa de volta a
 * `pending` com `next_attempt_at=now()` e lock liberado, para o worker do outbox
 * (process-notifications, S8) reprocessá-las com claim concorrência-safe. NÃO
 * envia diretamente (o envio provider-aware vive no NotificationService/worker).
 *
 * Body opcional: { delivery_id?: string } para reenviar uma entrega específica.
 */
export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  try {
    const { id } = await params;
    const authResult = await requireAttendanceAdmin(request);
    if (authResult.response) return authResult.response;
    const { auth } = authResult;

    const convResult = await resolveConversation(request, auth, id);
    if (convResult.response) return convResult.response;
    const { conversation } = convResult;

    let body: Record<string, unknown> = {};
    try {
      body = (await request.json()) as Record<string, unknown>;
    } catch {
      body = {};
    }

    let query = supabaseAdmin
      .from('notification_deliveries')
      .update({
        status: 'pending',
        next_attempt_at: new Date().toISOString(),
        locked_until: null,
        locked_by: null,
        last_error: null,
        updated_at: new Date().toISOString(),
      })
      .eq('conversation_id', conversation.id)
      .eq('company_id', auth.companyId)
      .in('status', ['failed', 'skipped'])
      // SÓ alertas de handoff são reenfileiráveis (§11.1/§11.4). NUNCA
      // 'human_message' (auditoria da msg ao cliente; o worker a despacharia como
      // alerta interno de handoff para o telefone do cliente).
      .in('event_type', ALERT_EVENT_TYPES);

    if (typeof body.delivery_id === 'string') {
      query = query.eq('id', body.delivery_id);
    }

    const { data, error } = await query.select('id');

    if (error) {
      return apiError('Erro ao reenfileirar notificações', {
        cause: error,
        logMessage: '[ATTENDANCE notifications/resend] update failed',
        request,
        status: 500,
      });
    }

    return NextResponse.json({ success: true, requeued: data?.length ?? 0 });
  } catch (error: unknown) {
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[ATTENDANCE notifications/resend] error',
      request,
      status: 500,
    });
  }
}
