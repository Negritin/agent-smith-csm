import { NextRequest, NextResponse } from 'next/server';
import { apiError, authApiError } from '@/lib/api-error';
import { auditCrossTenantAttempt, requireAdminSession } from '@/lib/auth-actions';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

export const dynamic = 'force-dynamic';

const supabaseAdmin = getSupabaseAdmin();

/**
 * POST /api/admin/handoff-recipients/[id]/test (§9.4).
 *
 * Dispara um envio de TESTE para o destinatário. O envio real é provider-aware e
 * vive no NotificationService (S4) — enfileiramos uma `notification_deliveries`
 * com event_type='test_notification' e status='pending' para o worker do outbox
 * (process-notifications, S8) entregá-la com o dispatcher correto (z-api/uazapi
 * ou SendGrid). NÃO fazemos envio direto aqui (evita acoplar provider no Next).
 *
 * NOTA: feedback imediato (sent/failed síncrono) exigiria um endpoint backend que
 * exponha o dispatcher do NotificationService — ver open_questions.
 */
export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  try {
    const { id } = await params;
    const result = await requireAdminSession();
    if (result.response) return authApiError(result.response, { request });
    const session = result.session;

    const { data: recipient, error } = await supabaseAdmin
      .from('handoff_notification_recipients')
      .select('id, company_id, agent_id, channel, recipient_value, enabled')
      .eq('id', id)
      .single();

    if (error || !recipient) {
      return apiError('Destinatário não encontrado', { request, status: 404 });
    }

    if (session.role === 'company_admin' && recipient.company_id !== session.companyId) {
      await auditCrossTenantAttempt({
        actorId: session.adminId,
        actorRole: session.role,
        actorCompanyId: session.companyId,
        resourceType: 'handoff_notification_recipients',
        resourceId: id,
        targetCompanyId: recipient.company_id,
        action: 'test_handoff_recipient',
        request,
      });
      return apiError('Destinatário não encontrado', { request, status: 404 });
    }

    if (!recipient.enabled) {
      return apiError('Destinatário desativado', { request, status: 400 });
    }

    const idempotencyKey = `test:${recipient.id}:${Date.now()}`;

    const { data: delivery, error: insertError } = await supabaseAdmin
      .from('notification_deliveries')
      .insert({
        company_id: recipient.company_id,
        recipient_id: recipient.id,
        event_type: 'test_notification',
        idempotency_key: idempotencyKey,
        channel: recipient.channel,
        recipient_value: recipient.recipient_value,
        status: 'pending',
        next_attempt_at: new Date().toISOString(),
      })
      .select('id, status')
      .single();

    if (insertError) {
      return apiError('Erro ao enfileirar teste', {
        cause: insertError,
        logMessage: '[HANDOFF RECIPIENT TEST] insert failed',
        request,
        status: 500,
      });
    }

    return NextResponse.json({
      success: true,
      delivery_id: delivery.id,
      status: delivery.status,
      message: 'Teste enfileirado; será entregue pelo worker de notificações.',
    });
  } catch (error: unknown) {
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[HANDOFF RECIPIENT TEST] error',
      request,
      status: 500,
    });
  }
}
