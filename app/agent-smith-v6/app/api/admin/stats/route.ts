import { NextRequest, NextResponse } from 'next/server';
import { apiError, authApiError } from '@/lib/api-error';
import { requireAdminSession } from '@/lib/auth-actions';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

/**
 * GET /api/admin/stats
 *
 * Returns dashboard statistics for admin panel.
 * Requires: smith_admin_session cookie
 */
export async function GET(request: NextRequest) {
  try {
    // =============================================
    // AUTHENTICATION CHECK
    // =============================================
    const auth = await requireAdminSession();
    if (auth.response) return authApiError(auth.response, { request });
    const { session } = auth;

    // =============================================
    // SERVICE ROLE CLIENT
    // =============================================
    const supabaseAdmin = getSupabaseAdmin();

    // =============================================
    // FETCH STATISTICS
    // =============================================
    const last24h = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();

    let companiesQuery = supabaseAdmin.from('companies').select('status, monthly_fee');
    let usersQuery = supabaseAdmin.from('users_v2').select('status');
    let logsQuery = supabaseAdmin
      .from('system_logs')
      .select('id', { count: 'exact', head: true })
      .gte('timestamp', last24h);
    let failedLoginsQuery = supabaseAdmin
      .from('system_logs')
      .select('id', { count: 'exact', head: true })
      .eq('action_type', 'LOGIN_FAILED')
      .gte('timestamp', last24h);
    let errorsQuery = supabaseAdmin
      .from('system_logs')
      .select('id', { count: 'exact', head: true })
      .eq('status', 'error')
      .gte('timestamp', last24h);
    let subscriptionsQuery = supabaseAdmin
      .from('subscriptions')
      .select('id, company_id, plans(price_brl)')
      .eq('status', 'active');

    if (session.role === 'company_admin') {
      companiesQuery = companiesQuery.eq('id', session.companyId);
      usersQuery = usersQuery.eq('company_id', session.companyId);
      logsQuery = logsQuery.eq('company_id', session.companyId);
      failedLoginsQuery = failedLoginsQuery.eq('company_id', session.companyId);
      errorsQuery = errorsQuery.eq('company_id', session.companyId);
      subscriptionsQuery = subscriptionsQuery.eq('company_id', session.companyId);
    }

    const [
      companiesResult,
      usersResult,
      logsResult,
      failedLoginsResult,
      errorsResult,
      subscriptionsResult,
    ] = await Promise.all([
      companiesQuery,
      usersQuery,
      logsQuery,
      failedLoginsQuery,
      errorsQuery,
      subscriptionsQuery,
    ]);

    // =============================================
    // CALCULATE STATS
    // =============================================
    const companies = companiesResult.data || [];
    const users = usersResult.data || [];
    const subscriptions = subscriptionsResult.data || [];

    // MRR = soma dos price_brl de todas as subscriptions ativas
    const mrr = subscriptions.reduce((sum, sub) => {
      const plan = sub.plans as any;
      const price = parseFloat(plan?.price_brl || '0');
      return sum + price;
    }, 0);

    const stats = {
      totalCompanies: companies.length,
      activeCompanies: companies.filter((c) => c.status === 'active').length,
      suspendedCompanies: companies.filter((c) => c.status === 'suspended').length,
      mrr: mrr,
      totalUsers: users.length,
      pendingUsers: users.filter((u) => u.status === 'pending').length,
      activeUsers: users.filter((u) => u.status === 'active').length,
      suspendedUsers: users.filter((u) => u.status === 'suspended').length,
      logsLast24h: logsResult.count || 0,
      failedLoginsLast24h: failedLoginsResult.count || 0,
      errorsLast24h: errorsResult.count || 0,
      // Adicionar contagem de subscriptions ativas
      activeSubscriptions: subscriptions.length,
    };

    return NextResponse.json(stats);
  } catch (error: unknown) {
    return apiError('Erro interno ao buscar estatísticas', {
      cause: error,
      logMessage: '[ADMIN STATS] Error',
      request,
      status: 500,
    });
  }
}
