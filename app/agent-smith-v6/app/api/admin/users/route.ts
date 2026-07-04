import { NextRequest, NextResponse } from 'next/server';
import { apiError, authApiError } from '@/lib/api-error';
import { requireAdminSession } from '@/lib/auth-actions';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

/**
 * GET /api/admin/users
 *
 * Returns list of all users WITHOUT sensitive fields.
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
    // FETCH USERS (WITHOUT SENSITIVE FIELDS)
    // =============================================
    // IMPORTANT: Never include password_hash, reset_token, etc.
    let query = supabaseAdmin
      .from('users_v2')
      .select(
        'id, email, first_name, last_name, role, status, company_id, created_at, phone, cpf, is_owner',
      )
      .order('created_at', { ascending: false });

    if (session.role === 'company_admin') {
      query = query.eq('company_id', session.companyId);
    }

    const { data: users, error } = await query;

    if (error) {
      return apiError('Erro ao buscar usuários', {
        cause: error,
        logMessage: '[ADMIN USERS] Error fetching users',
        request,
        status: 500,
      });
    }

    return NextResponse.json({ users: users || [] });
  } catch (error: unknown) {
    return apiError('Erro interno ao buscar usuários', {
      cause: error,
      logMessage: '[ADMIN USERS] Error',
      request,
      status: 500,
    });
  }
}
