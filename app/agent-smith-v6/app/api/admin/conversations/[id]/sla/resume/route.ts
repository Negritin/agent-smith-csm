import { NextRequest, NextResponse } from 'next/server';
import { apiError } from '@/lib/api-error';
import { requireAttendanceAdmin, resolveConversation } from '@/lib/attendance-actions';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

export const dynamic = 'force-dynamic';

const supabaseAdmin = getSupabaseAdmin();

/**
 * POST /api/admin/conversations/[id]/sla/resume (§9.1)
 *
 * Retoma o SLA pausado: acumula o tempo pausado em `paused_duration_seconds`,
 * limpa `paused_at` e devolve o health a `within_sla` (o worker de SLA, S8,
 * recomputa o threshold real no próximo tick). NÃO toca `conversations.status`.
 *
 * NOTA: a recomputação fina de health/deadlines após retomada pertence ao
 * SlaService (S3)/worker (S8) — ver open_questions.
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

    const { data: sla, error: slaError } = await supabaseAdmin
      .from('attendance_sla')
      .select('id, health_status, paused_at, paused_duration_seconds')
      .eq('conversation_id', conversation.id)
      .eq('company_id', auth.companyId)
      .is('resolved_at', null)
      .order('created_at', { ascending: false })
      .limit(1)
      .maybeSingle();

    if (slaError) {
      return apiError('Erro ao buscar SLA', {
        cause: slaError,
        logMessage: '[ATTENDANCE sla/resume] select failed',
        request,
        status: 500,
      });
    }
    if (!sla) {
      return apiError('SLA não configurado para esta conversa', { request, status: 404 });
    }
    if (sla.health_status !== 'paused') {
      return NextResponse.json({ success: true, already_active: true });
    }

    const pausedAtMs = sla.paused_at ? new Date(sla.paused_at).getTime() : Date.now();
    const accumulated = Number(sla.paused_duration_seconds || 0);
    const extraSeconds = Math.max(0, Math.round((Date.now() - pausedAtMs) / 1000));

    const { error: updateError } = await supabaseAdmin
      .from('attendance_sla')
      .update({
        health_status: 'within_sla',
        paused_at: null,
        paused_duration_seconds: accumulated + extraSeconds,
        updated_at: new Date().toISOString(),
      })
      .eq('id', sla.id)
      .eq('company_id', auth.companyId);

    if (updateError) {
      return apiError('Erro ao retomar SLA', {
        cause: updateError,
        logMessage: '[ATTENDANCE sla/resume] update failed',
        request,
        status: 500,
      });
    }

    return NextResponse.json({ success: true });
  } catch (error: unknown) {
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[ATTENDANCE sla/resume] error',
      request,
      status: 500,
    });
  }
}
