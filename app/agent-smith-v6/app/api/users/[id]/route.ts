import { NextRequest, NextResponse } from 'next/server';
import { getUserOrAdminSession } from '@/lib/auth-actions';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

const supabaseAdmin = getSupabaseAdmin();

/**
 * GET /api/users/[id]
 *
 * Returns basic user info for displaying sender name/avatar.
 * Used by Realtime message enrichment.
 * Requires: smith_admin_session cookie
 */
export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  try {
    const { id } = await params;

    // =============================================
    // AUTHENTICATION CHECK (USER OR ADMIN)
    // =============================================
    const auth = await getUserOrAdminSession();
    if (auth.response) return auth.response;

    if (!id) {
      return NextResponse.json({ error: 'ID é obrigatório' }, { status: 400 });
    }

    if (auth.userSession && id !== auth.userSession.userId) {
      return NextResponse.json({ error: 'Usuário não encontrado' }, { status: 404 });
    }

    // =============================================
    // FETCH USER BASIC INFO
    // =============================================
    let query = supabaseAdmin
      .from('users_v2')
      .select('id, first_name, last_name, avatar_url, company_id')
      .eq('id', id);

    if (auth.adminSession?.role === 'company_admin') {
      query = query.eq('company_id', auth.adminSession.companyId);
    }

    const { data, error } = await query.single();

    if (error) {
      console.error('[USERS API] Error:', error);
      return NextResponse.json({ error: 'Usuário não encontrado' }, { status: 404 });
    }

    const { company_id, ...safeUser } = data;
    return NextResponse.json({ user: safeUser });
  } catch (error: any) {
    console.error('[USERS API] Error:', error);
    return NextResponse.json({ error: 'Erro interno' }, { status: 500 });
  }
}
