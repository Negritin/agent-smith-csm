import { NextRequest, NextResponse } from 'next/server';
import { apiError } from '@/lib/api-error';
import { requireOwnerOrMaster } from '@/lib/auth-actions';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

export const dynamic = 'force-dynamic';

const supabaseAdmin = getSupabaseAdmin();

/**
 * Constrói os bounds de dia em horário do Brasil (GMT-3), mirror de
 * billing.py:207-217. Aceita start_date/end_date (YYYY-MM-DD) OU `days` (default 30).
 * Retorna [start, end) — half-open: end = (end_date + 1 dia) 00:00 -03:00.
 */
function resolveRange(params: URLSearchParams): { start: string; end: string } {
  const startDate = params.get('start_date');
  const endDate = params.get('end_date');
  if (startDate && endDate) {
    const endNext = new Date(`${endDate}T00:00:00-03:00`);
    endNext.setUTCDate(endNext.getUTCDate() + 1);
    return {
      start: `${startDate}T00:00:00-03:00`,
      end: endNext.toISOString(),
    };
  }
  const days = Math.max(1, parseInt(params.get('days') || '30', 10) || 30);
  const now = new Date();
  const start = new Date(now);
  start.setUTCDate(start.getUTCDate() - days);
  return { start: start.toISOString(), end: now.toISOString() };
}

/**
 * GET /api/admin/metrics/by-agent — aba "Agentes" (SPEC §5).
 * Owner-gate server-side: só Owner (company_admin) ou master_admin (com company_id).
 */
export async function GET(request: NextRequest) {
  try {
    const gate = await requireOwnerOrMaster(request);
    if (gate.response) return gate.response;
    const companyId = gate.companyId;

    const { start, end } = resolveRange(new URL(request.url).searchParams);

    const { data, error } = await supabaseAdmin.rpc('rpc_metrics_by_agent', {
      p_company_id: companyId,
      p_start: start,
      p_end: end,
    });
    if (error) {
      return apiError('Erro ao carregar métricas por agente', {
        cause: error,
        logMessage: '[METRICS] by-agent RPC failed',
        request,
        status: 500,
      });
    }

    return NextResponse.json({ by_agent: data ?? [] });
  } catch (err) {
    return apiError('Erro ao carregar métricas por agente', {
      cause: err,
      logMessage: '[METRICS] by-agent unexpected error',
      request,
      status: 500,
    });
  }
}
