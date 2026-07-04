import { NextRequest, NextResponse } from 'next/server';
import { cookies } from 'next/headers';
import { getIronSession } from 'iron-session';
import { createClient } from '@supabase/supabase-js';
import { sessionOptions, SessionData } from '@/lib/iron-session';

export const dynamic = 'force-dynamic';

/**
 * GET /api/user/company-data
 *
 * Returns the logged-in user's company_id and company settings (allow_web_search).
 * Requires: smith_user_session cookie
 */
export async function GET(request: NextRequest) {
  try {
    // =============================================
    // AUTHENTICATION CHECK
    // =============================================
    const cookieStore = await cookies();
    const session = await getIronSession<SessionData>(cookieStore, sessionOptions);

    if (!session.userId) {
      return NextResponse.json({ error: 'Não autorizado' }, { status: 401 });
    }

    const userId = session.userId;

    // =============================================
    // SERVICE ROLE CLIENT
    // =============================================
    const supabaseAdmin = createClient(
      process.env.NEXT_PUBLIC_SUPABASE_URL!,
      process.env.SUPABASE_SERVICE_ROLE_KEY!,
      { auth: { persistSession: false } },
    );

    // =============================================
    // FETCH USER -> COMPANY DATA
    // =============================================
    const { data: userData, error: userError } = await supabaseAdmin
      .from('users_v2')
      .select('company_id')
      .eq('id', userId)
      .single();

    if (userError || !userData?.company_id) {
      return NextResponse.json({ error: 'Empresa não encontrada' }, { status: 404 });
    }

    const { data: companyData, error: companyError } = await supabaseAdmin
      .from('companies')
      .select('id, allow_web_search')
      .eq('id', userData.company_id)
      .single();

    if (companyError) {
      return NextResponse.json({ error: 'Erro ao buscar dados da empresa' }, { status: 500 });
    }

    return NextResponse.json({
      companyId: userData.company_id,
      allowWebSearch: companyData?.allow_web_search || false,
    });
  } catch (error: any) {
    console.error('[USER COMPANY DATA] Error:', error);
    return NextResponse.json({ error: 'Erro interno' }, { status: 500 });
  }
}
