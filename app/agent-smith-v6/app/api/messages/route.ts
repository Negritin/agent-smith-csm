import { NextRequest, NextResponse } from 'next/server';
import { getUserOrAdminSession } from '@/lib/auth-actions';
import { getSupabaseAdmin } from '@/lib/supabase-admin';
import { messageIsHuman } from '@/types/conversation-details';

const supabaseAdmin = getSupabaseAdmin();

async function validateConversationAccess(
  conversationId: string,
  auth: Awaited<ReturnType<typeof getUserOrAdminSession>>,
): Promise<NextResponse | null> {
  if (auth.response) return auth.response;

  let query = supabaseAdmin.from('conversations').select('id, user_id, company_id').eq('id', conversationId);

  if (auth.userSession) {
    query = query.eq('user_id', auth.userSession.userId);
  } else if (auth.adminSession?.role === 'company_admin') {
    query = query.eq('company_id', auth.adminSession.companyId);
  }

  const { data: conversation, error } = await query.single();

  if (error || !conversation) {
    return NextResponse.json({ error: 'Conversa não encontrada' }, { status: 404 });
  }

  return null;
}

/**
 * POST /api/messages
 *
 * Creates a new message in a conversation.
 * Requires: smith_user_session OR smith_admin_session cookie
 */
export async function POST(request: NextRequest) {
  try {
    // =============================================
    // AUTHENTICATION CHECK (USER OR ADMIN)
    // =============================================
    const auth = await getUserOrAdminSession();
    if (auth.response) return auth.response;

    // =============================================
    // VALIDATE INPUT
    // =============================================
    const body = await request.json();
    const { conversation_id, role, content, type, audio_url, image_url, metadata } = body;

    if (!conversation_id) {
      return NextResponse.json({ error: 'conversation_id é obrigatório' }, { status: 400 });
    }

    if (!role || !content) {
      return NextResponse.json({ error: 'role e content são obrigatórios' }, { status: 400 });
    }

    const accessError = await validateConversationAccess(conversation_id, auth);
    if (accessError) return accessError;

    // =============================================
    // CREATE MESSAGE
    // =============================================
    const { data, error } = await supabaseAdmin
      .from('messages')
      .insert({
        conversation_id,
        role,
        content,
        type: type || 'text',
        audio_url: audio_url || metadata?.audio_url || null,
        image_url: image_url || metadata?.image_url || null,
      })
      .select()
      .single();

    if (error) {
      console.error('[MESSAGES API] Error creating message:', error);
      return NextResponse.json({ error: 'Erro ao criar mensagem' }, { status: 500 });
    }

    // Update conversation updated_at
    await supabaseAdmin
      .from('conversations')
      .update({ updated_at: new Date().toISOString() })
      .eq('id', conversation_id);

    return NextResponse.json({ message: data }, { status: 201 });
  } catch (error: any) {
    console.error('[MESSAGES API] Error:', error);
    return NextResponse.json({ error: 'Erro interno ao criar mensagem' }, { status: 500 });
  }
}

/**
 * GET /api/messages?conversation_id=xxx
 *
 * Gets all messages for a conversation.
 * Requires: smith_user_session OR smith_admin_session cookie
 */
export async function GET(request: NextRequest) {
  try {
    // =============================================
    // AUTHENTICATION CHECK
    // scope=admin (painel de atendimento) prioriza a sessão de ADMIN sobre um
    // cookie smith_user_session residual — senão escopa por user_id e dá 404 em
    // conversa de outro user (ex.: contato do WhatsApp). Sem scope (dashboard do
    // usuário final) mantém o user-first padrão.
    // =============================================
    const { searchParams } = new URL(request.url);
    const auth = await getUserOrAdminSession(searchParams.get('scope') === 'admin');
    if (auth.response) return auth.response;

    // =============================================
    // GET CONVERSATION ID FROM QUERY
    // =============================================
    const conversationId = searchParams.get('conversation_id');

    if (!conversationId) {
      return NextResponse.json({ error: 'conversation_id é obrigatório' }, { status: 400 });
    }

    const accessError = await validateConversationAccess(conversationId, auth);
    if (accessError) return accessError;

    // =============================================
    // FETCH MESSAGES WITH SENDER INFO
    // Usa left join para trazer dados do admin que enviou (sender_user_id)
    // =============================================
    // console.log('[MESSAGES API] Fetching messages for conversation:', conversationId);

    const { data, error } = await supabaseAdmin
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
      .eq('conversation_id', conversationId)
      .order('created_at', { ascending: true });

    if (error) {
      console.error('[MESSAGES API] Error fetching messages:', error);
      return NextResponse.json({ error: 'Erro ao buscar mensagens' }, { status: 500 });
    }

    // FONTE ÚNICA da autoria humana (§22 item 3): projeta `is_human` derivado de
    // role/author_type/sender_user_id via `messageIsHuman`, em vez de cada
    // consumidor (timeline admin/dashboard) reimplementar a regra e divergir
    // (ex.: mensagem legada role='assistant'+sender_user_id sem JOIN/prefixo era
    // renderizada como IA). Esta rota é autenticada (admin/dashboard); o widget
    // usa GET /api/widget/messages, que serve o cliente final (sem operadores).
    const messages = (data || []).map((m: any) => ({
      ...m,
      is_human: messageIsHuman({
        role: m.role ?? null,
        author_type: m.author_type ?? null,
        sender_user_id: m.sender_user_id ?? null,
      }),
    }));

    return NextResponse.json({ messages });
  } catch (error: any) {
    console.error('[MESSAGES API] Error:', error);
    return NextResponse.json({ error: 'Erro interno' }, { status: 500 });
  }
}
