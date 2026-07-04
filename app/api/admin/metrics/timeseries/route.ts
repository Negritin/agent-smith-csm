import { NextRequest, NextResponse } from 'next/server';
import { apiError } from '@/lib/api-error';
import { requireOwnerOrMaster } from '@/lib/auth-actions';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

export const dynamic = 'force-dynamic';

const supabaseAdmin = getSupabaseAdmin();

function resolveRange(params: URLSearchParams): { start: string; end: string } {
  const startDate = params.get('start_date');
  const endDate = params.get('end_date');
  if (startDate && endDate) {
    const endNext = new Date(`${endDate}T00:00:00-03:00`);
    endNext.setUTCDate(endNext.getUTCDate() + 1);
    return { start: `${startDate}T00:00:00-03:00`, end: endNext.toISOString() };
  }
  const days = Math.max(1, parseInt(params.get('days') || '30', 10) || 30);
  const now = new Date();
  const start = new Date(now);
  start.setUTCDate(start.getUTCDate() - days);
  return { start: start.toISOString(), end: now.toISOString() };
}

/**
 * GET /api/admin/metrics/timeseries — [{date, conversations, messages}] por dia,
 * gaps preenchidos com 0 (SPEC §2.3). Owner-gated.
 */
export async function GET(request: NextRequest) {
  try {
    const gate = await requireOwnerOrMaster(request);
    if (gate.response) return gate.response;
    const companyId = gate.companyId;

    const { start, end } = resolveRange(new URL(request.url).searchParams);

    const { data, error } = await supabaseAdmin.rpc('rpc_metrics_timeseries', {
      p_company_id: companyId,
      p_start: start,
      p_end: end,
    });
    if (error) {
      return apiError('Erro ao carregar série temporal', {
        cause: error,
        logMessage: '[METRICS] timeseries RPC failed',
        request,
        status: 500,
      });
    }

    return NextResponse.json(data ?? []);
  } catch (err) {
    return apiError('Erro ao carregar série temporal', {
      cause: err,
      logMessage: '[METRICS] timeseries unexpected error',
      request,
      status: 500,
    });
  }
}
