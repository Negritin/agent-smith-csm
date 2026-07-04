import { NextRequest, NextResponse } from 'next/server';
import { requireAdminSession } from '@/lib/auth-actions';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

const supabaseAdmin = getSupabaseAdmin();

/**
 * GET /api/admin/conversation-logs/data
 * Returns all data needed for conversation logs page: companies, agents, users
 */
export async function GET(request: NextRequest) {
  try {
    const auth = await requireAdminSession();
    if (auth.response) return auth.response;
    const { session } = auth;

    // Fetch all related data
    let companiesQuery = supabaseAdmin
      .from('companies')
      .select('id, company_name')
      .order('company_name');
    let agentsQuery = supabaseAdmin.from('agents').select('id, name, company_id');
    let usersQuery = supabaseAdmin.from('users_v2').select('id, email, first_name, last_name, company_id');

    if (session.role === 'company_admin') {
      companiesQuery = companiesQuery.eq('id', session.companyId);
      agentsQuery = agentsQuery.eq('company_id', session.companyId);
      usersQuery = usersQuery.eq('company_id', session.companyId);
    }

    const [companiesResult, agentsResult, usersResult] = await Promise.all([
      companiesQuery,
      agentsQuery,
      usersQuery,
    ]);

    if (companiesResult.error) {
      console.error('[CONV LOGS DATA] Companies error:', companiesResult.error);
    }

    return NextResponse.json({
      companies: companiesResult.data || [],
      agents: agentsResult.data || [],
      users: usersResult.data || [],
    });
  } catch (error: any) {
    console.error('[CONV LOGS DATA] Error:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
