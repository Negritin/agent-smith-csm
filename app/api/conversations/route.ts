import { NextRequest, NextResponse } from 'next/server';
import { cookies } from 'next/headers';
import { getIronSession } from 'iron-session';
import { createClient } from '@supabase/supabase-js';
import { sessionOptions, SessionData } from '@/lib/iron-session';
import { messageIsHuman } from '@/types/conversation-details';

export const dynamic = 'force-dynamic';

/**
 * POST /api/conversations
 *
 * Creates a new conversation for the authenticated user.
 * Requires: smith_user_session cookie
 */
export async function POST(request: NextRequest) {
  try {
    // =============================================
    // AUTHENTICATION CHECK (USER SESSION)
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
    // VALIDATE INPUT
    // =============================================
    const body = await request.json();
    const { title, session_id, agent_id } = body;

    // =============================================
    // GET USER'S COMPANY
    // =============================================
    const { data: userData, error: userError } = await supabaseAdmin
      .from('users_v2')
      .select('company_id')
      .eq('id', userId)
      .single();

    if (userError || !userData?.company_id) {
      return NextResponse.json({ error: 'Empresa do usuário não encontrada' }, { status: 400 });
    }

    // =============================================
    // CREATE CONVERSATION
    // =============================================
    const { data, error } = await supabaseAdmin
      .from('conversations')
      .insert({
        user_id: userId,
        company_id: userData.company_id,
        agent_id: agent_id || null,
        session_id: session_id || null,
        title: title || 'Nova Conversa',
        // Status canônico (§6.1): 'open' é o estado inicial. 'active' NÃO é canônico
        // e violaria conversations_status_check (S1). Ver 20260621_08_*.
        status: 'open',
      })
      .select()
      .single();

    if (error) {
      console.error('[CONVERSATIONS API] Error creating conversation:', error);
      return NextResponse.json({ error: 'Erro ao criar conversa' }, { status: 500 });
    }

    return NextResponse.json({ conversation: data }, { status: 201 });
  } catch (error: any) {
    console.error('[CONVERSATIONS API] Error:', error);
    return NextResponse.json({ error: 'Erro interno ao criar conversa' }, { status: 500 });
  }
}

/**
 * GET /api/conversations
 *
 * Gets all conversations for the authenticated user.
 * Requires: smith_user_session cookie
 */
export async function GET(request: NextRequest) {
  try {
    // =============================================
    // AUTHENTICATION CHECK (USER SESSION)
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
    // GET QUERY PARAMS
    // =============================================
    const { searchParams } = new URL(request.url);
    const sessionId = searchParams.get('session_id');

    // =============================================
    // FETCH CONVERSATIONS
    // =============================================
    if (sessionId) {
      // Fetch specific conversation by session_id with messages
      const { data: conversation, error } = await supabaseAdmin
        .from('conversations')
        .select('id, agent_id, session_id, status, title, created_at, updated_at')
        .eq('session_id', sessionId)
        .eq('user_id', userId)
        .maybeSingle();

      if (error) {
        console.error('[CONVERSATIONS API] Error fetching conversation:', error);
        return NextResponse.json({ error: 'Erro ao buscar conversa' }, { status: 500 });
      }

      if (!conversation) {
        return NextResponse.json({ conversation: null, messages: [] });
      }

      // Fetch messages for this conversation.
      // Espelha GET /api/messages: traz o JOIN `sender` (nome/avatar do operador)
      // e projeta `is_human` como FONTE ÚNICA da autoria humana (§22 item 3), em
      // vez de o consumidor (dashboard/MessageBubble) reimplementar a regra via
      // fallback e divergir. Sem isto, o carregamento INICIAL do dashboard
      // exibiria a mensagem humana legada (role='assistant'+sender_user_id, sem
      // prefixo/JOIN) como IA — e o polling por /api/messages não corrige, pois
      // só mescla ids novos.
      const { data: messages, error: messagesError } = await supabaseAdmin
        .from('messages')
        .select(
          `
                    *,
                    sender:sender_user_id (
                        first_name,
                        last_name,
                        avatar_url
                    )
                `,
        )
        .eq('conversation_id', conversation.id)
        .order('created_at', { ascending: true });

      if (messagesError) {
        console.error('[CONVERSATIONS API] Error fetching messages:', messagesError);
      }

      const projectedMessages = (messages || []).map((m: any) => ({
        ...m,
        is_human: messageIsHuman({
          role: m.role ?? null,
          author_type: m.author_type ?? null,
          sender_user_id: m.sender_user_id ?? null,
        }),
      }));

      return NextResponse.json({
        conversation,
        messages: projectedMessages,
      });
    }

    // Get limit from query params (default 50)
    const limitParam = searchParams.get('limit');
    const limit = limitParam ? parseInt(limitParam, 10) : 50;
    const includeCounts = searchParams.get('include_counts') === 'true';

    // Fetch all conversations for user with agent info.
    // `agents:agent_id (name)` desambigua o embed: a tabela conversations tem 2 FKs
    // para agents (agent_id e closed_by_agent_id, esta adicionada no attendance), então
    // o embed cru `agents(name)` vira ambíguo (PGRST201) e a rota dá 500. Mesma
    // convenção já usada nos admin routes (agents:agent_id (...)).
    const { data, error } = await supabaseAdmin
      .from('conversations')
      .select('*, agents:agent_id (name)')
      .eq('user_id', userId)
      .order('updated_at', { ascending: false })
      .limit(limit);

    if (error) {
      console.error('[CONVERSATIONS API] Error fetching conversations:', error);
      return NextResponse.json({ error: 'Erro ao buscar conversas' }, { status: 500 });
    }

    // Add message counts if requested
    let conversationsWithCounts = data || [];
    if (includeCounts && data) {
      conversationsWithCounts = await Promise.all(
        data.map(async (conv) => {
          const { count } = await supabaseAdmin
            .from('messages')
            .select('*', { count: 'exact', head: true })
            .eq('conversation_id', conv.id);
          return { ...conv, message_count: count || 0 };
        }),
      );
    }

    return NextResponse.json({ conversations: conversationsWithCounts });
  } catch (error: any) {
    console.error('[CONVERSATIONS API] Error:', error);
    return NextResponse.json({ error: 'Erro interno ao buscar conversas' }, { status: 500 });
  }
}
