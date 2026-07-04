import { NextRequest, NextResponse } from 'next/server';
import { cookies } from 'next/headers';
import { getIronSession } from 'iron-session';
import { createClient } from '@supabase/supabase-js';
import { sessionOptions, SessionData } from '@/lib/iron-session';

export const dynamic = 'force-dynamic';

/**
 * GET /api/agents
 *
 * Lists active agents for the logged-in user's company.
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
    // GET USER'S COMPANY
    // =============================================
    const { data: userData, error: userError } = await supabaseAdmin
      .from('users_v2')
      .select('company_id')
      .eq('id', userId)
      .single();

    if (userError || !userData?.company_id) {
      return NextResponse.json({ error: 'Empresa não encontrada' }, { status: 404 });
    }

    // =============================================
    // FETCH ACTIVE AGENTS
    // =============================================
    const { data: agents, error: agentsError } = await supabaseAdmin
      .from('agents')
      .select('id, name, is_subagent, allow_direct_chat')
      .eq('company_id', userData.company_id)
      .eq('is_active', true)
      .order('created_at', { ascending: true });

    if (agentsError) {
      console.error('[AGENTS API] Error:', agentsError);
      return NextResponse.json({ error: 'Erro ao buscar agentes' }, { status: 500 });
    }

    // Filter out subagents that don't have allow_direct_chat enabled
    const chatAgents = (agents || []).filter(
      (a: any) => !a.is_subagent || a.allow_direct_chat
    );

    return NextResponse.json({ agents: chatAgents });
  } catch (error: any) {
    console.error('[AGENTS API] Error:', error);
    return NextResponse.json({ error: 'Erro interno' }, { status: 500 });
  }
}
