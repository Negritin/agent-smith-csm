import { NextRequest, NextResponse } from 'next/server';
import { apiError } from '@/lib/api-error';
import { requireAttendanceAdmin, resolveConversation } from '@/lib/attendance-actions';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

export const dynamic = 'force-dynamic';

const supabaseAdmin = getSupabaseAdmin();

/**
 * POST /api/admin/conversations/[id]/sla/pause (§9.1)
 *
 * Pausa o SLA da sessão atual (health_status='paused', paused_at=now()). NÃO toca
 * `conversations.status` (D1 cobre apenas status; SLA é write separado, escopado
 * por company_id). Idempotente: se já está pausado, no-op.
 *
 * NOTA: a contabilização precisa de tempo pausado / recomputação fina de health
 * pertence ao SlaService (S3), que ainda não expõe pause/resume — ver open_questions.
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

    // SLA da sessão atual da conversa (escopo company_id + ainda não pausado).
    const { data: sla, error: slaError } = await supabaseAdmin
      .from('attendance_sla')
      .select('id, health_status, paused_at')
      .eq('conversation_id', conversation.id)
      .eq('company_id', auth.companyId)
      .is('resolved_at', null)
      .order('created_at', { ascending: false })
      .limit(1)
      .maybeSingle();

    if (slaError) {
      return apiError('Erro ao buscar SLA', {
        cause: slaError,
        logMessage: '[ATTENDANCE sla/pause] select failed',
        request,
        status: 500,
      });
    }
    if (!sla) {
      return apiError('SLA não configurado para esta conversa', { request, status: 404 });
    }
    if (sla.health_status === 'paused') {
      return NextResponse.json({ success: true, already_paused: true });
    }

    const { error: updateError } = await supabaseAdmin
      .from('attendance_sla')
      .update({
        health_status: 'paused',
        paused_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      })
      .eq('id', sla.id)
      .eq('company_id', auth.companyId);

    if (updateError) {
      return apiError('Erro ao pausar SLA', {
        cause: updateError,
        logMessage: '[ATTENDANCE sla/pause] update failed',
        request,
        status: 500,
      });
    }

    return NextResponse.json({ success: true });
  } catch (error: unknown) {
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[ATTENDANCE sla/pause] error',
      request,
      status: 500,
    });
  }
}
