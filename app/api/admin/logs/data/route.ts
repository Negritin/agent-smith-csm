import { NextRequest, NextResponse } from 'next/server';
import { requireAdminSession } from '@/lib/auth-actions';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

/**
 * GET /api/admin/logs/data
 *
 * Returns system logs and related entity data for the admin logs page.
 * Requires: smith_admin_session cookie
 *
 * Query params:
 * - dateFilter: 'today' | '7days' | '30days' | '90days'
 */
export async function GET(request: NextRequest) {
  try {
    // =============================================
    // AUTHENTICATION CHECK
    // =============================================
    const auth = await requireAdminSession();
    if (auth.response) return auth.response;
    const { session } = auth;

    // =============================================
    // SERVICE ROLE CLIENT
    // =============================================
    const supabaseAdmin = getSupabaseAdmin();

    // =============================================
    // GET DATE FILTER FROM QUERY
    // =============================================
    const { searchParams } = new URL(request.url);
    const dateFilter = searchParams.get('dateFilter') || '7days';
    const dateThreshold = getDateThreshold(dateFilter);

    // =============================================
    // FETCH DATA
    // =============================================
    let logsQuery = supabaseAdmin
      .from('system_logs')
      .select('*')
      .gte('timestamp', dateThreshold)
      .order('timestamp', { ascending: false })
      .limit(1000);
    let usersQuery = supabaseAdmin
      .from('users_v2')
      .select('id, email, first_name, last_name, company_id');
    let companiesQuery = supabaseAdmin.from('companies').select('id, company_name');

    if (session.role === 'company_admin') {
      logsQuery = logsQuery.eq('company_id', session.companyId);
      usersQuery = usersQuery.eq('company_id', session.companyId);
      companiesQuery = companiesQuery.eq('id', session.companyId);
    }

    const [logsResult, usersResult, adminsResult, companiesResult] = await Promise.all([
      logsQuery,
      // IMPORTANT: Never include password_hash or sensitive fields
      usersQuery,
      session.role === 'master_admin'
        ? supabaseAdmin.from('admin_users').select('id, email, name')
        : Promise.resolve({ data: [], error: null }),
      companiesQuery,
    ]);

    if (logsResult.error) {
      console.error('[ADMIN LOGS DATA] Error fetching logs:', logsResult.error);
    }

    // =============================================
    // BUILD LOOKUP MAPS
    // =============================================
    const usersMap: Record<string, any> = {};
    if (usersResult.data) {
      usersResult.data.forEach((user) => {
        usersMap[user.id] = user;
      });
    }

    const adminsMap: Record<string, any> = {};
    if (adminsResult.data) {
      adminsResult.data.forEach((admin) => {
        adminsMap[admin.id] = admin;
      });
    }

    const companiesMap: Record<string, any> = {};
    if (companiesResult.data) {
      companiesResult.data.forEach((company) => {
        companiesMap[company.id] = company;
      });
    }

    return NextResponse.json({
      logs: logsResult.data || [],
      users: usersMap,
      admins: adminsMap,
      companies: companiesMap,
    });
  } catch (error: any) {
    console.error('[ADMIN LOGS DATA] Error:', error);
    return NextResponse.json({ error: 'Erro interno ao buscar logs' }, { status: 500 });
  }
}

/**
 * Calculate date threshold based on filter
 */
function getDateThreshold(filter: string): string {
  const now = new Date();
  switch (filter) {
    case 'today':
      now.setHours(0, 0, 0, 0);
      return now.toISOString();
    case '7days':
      now.setDate(now.getDate() - 7);
      return now.toISOString();
    case '30days':
      now.setDate(now.getDate() - 30);
      return now.toISOString();
    case '90days':
      now.setDate(now.getDate() - 90);
      return now.toISOString();
    default:
      now.setDate(now.getDate() - 7);
      return now.toISOString();
  }
}
