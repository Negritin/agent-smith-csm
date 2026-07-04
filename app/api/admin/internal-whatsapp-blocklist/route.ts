import { NextRequest, NextResponse } from 'next/server';
import { apiError, authApiError } from '@/lib/api-error';
import { requireAdminSession } from '@/lib/auth-actions';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

export const dynamic = 'force-dynamic';

const supabaseAdmin = getSupabaseAdmin();

/**
 * GET /api/admin/internal-whatsapp-blocklist (§9.4).
 *
 * Lista os números internos bloqueados da empresa (operadores que recebem alerta
 * de handoff e não devem disparar a IA ao responder, §8.4). Read-only escopado por
 * company_id. A sincronização (criar/desativar) acontece via handoff-recipients.
 */

const BLOCKLIST_COLUMNS =
  'id, company_id, agent_id, integration_id, phone_normalized, source_recipient_id, ' +
  'reason, active, block_count, last_blocked_at, created_at';

export async function GET(request: NextRequest) {
  try {
    const result = await requireAdminSession();
    if (result.response) return authApiError(result.response, { request });
    const session = result.session;

    let companyId: string;
    if (session.role === 'company_admin') {
      if (!session.companyId) {
        return apiError('Não autorizado', { request, status: 403 });
      }
      companyId = session.companyId;
    } else {
      const url = new URL(request.url);
      const queryCompanyId =
        url.searchParams.get('company_id') || url.searchParams.get('companyId');
      if (!queryCompanyId) {
        return apiError('company_id é obrigatório para master_admin', { request, status: 400 });
      }
      companyId = queryCompanyId;
    }

    const url = new URL(request.url);
    const activeOnly = url.searchParams.get('active') !== 'all';

    let query = supabaseAdmin
      .from('internal_whatsapp_blocklist')
      .select(BLOCKLIST_COLUMNS)
      .eq('company_id', companyId)
      .order('created_at', { ascending: false });

    if (activeOnly) query = query.eq('active', true);

    const { data, error } = await query;
    if (error) {
      return apiError('Erro ao listar blocklist', {
        cause: error,
        logMessage: '[BLOCKLIST] select failed',
        request,
        status: 500,
      });
    }

    return NextResponse.json({ blocklist: data ?? [] });
  } catch (error: unknown) {
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[BLOCKLIST] GET error',
      request,
      status: 500,
    });
  }
}
