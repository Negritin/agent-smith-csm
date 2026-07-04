import { NextRequest, NextResponse } from 'next/server';
import { apiError } from '@/lib/api-error';
import { requireAttendanceAdmin } from '@/lib/attendance-actions';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

export const dynamic = 'force-dynamic';

const supabaseAdmin = getSupabaseAdmin();

/** Estados por fila — espelham o filtro de status de GET /api/admin/conversations. */
const HUMAN_ACTIVE_STATUSES = ['HUMAN_ACTIVE', 'PENDING_CUSTOMER'];
const FINALIZED_STATUSES = ['RESOLVED', 'CLOSED'];

/**
 * GET /api/admin/conversations/counts
 *
 * Contadores por fila para o seletor do inbox. Usa `count: 'exact', head: true`
 * (não transfere linhas, sem enriquecimento de SLA) — uma query barata por fila,
 * todas escopadas por `company_id`. Mantém as 4 filas + o total da empresa.
 */
export async function GET(request: NextRequest) {
  try {
    const authResult = await requireAttendanceAdmin(request);
    if (authResult.response) return authResult.response;
    const companyId = authResult.auth.companyId;

    const base = () =>
      supabaseAdmin
        .from('conversations')
        .select('id', { count: 'exact', head: true })
        .eq('company_id', companyId);

    const [agente, humano, naoRespondido, finalizado, total] = await Promise.all([
      base().eq('status', 'open'),
      base().in('status', HUMAN_ACTIVE_STATUSES),
      base().eq('status', 'HUMAN_REQUESTED'),
      base().in('status', FINALIZED_STATUSES),
      base(),
    ]);

    const firstError =
      agente.error || humano.error || naoRespondido.error || finalizado.error || total.error;
    if (firstError) {
      return apiError('Erro ao contar conversas', {
        cause: firstError,
        logMessage: '[ADMIN CONVERSATIONS COUNTS] query error',
        request,
        status: 500,
      });
    }

    return NextResponse.json({
      agente: agente.count ?? 0,
      humano: humano.count ?? 0,
      nao_respondido: naoRespondido.count ?? 0,
      finalizado: finalizado.count ?? 0,
      total: total.count ?? 0,
    });
  } catch (error: unknown) {
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[ADMIN CONVERSATIONS COUNTS] error',
      request,
      status: 500,
    });
  }
}
