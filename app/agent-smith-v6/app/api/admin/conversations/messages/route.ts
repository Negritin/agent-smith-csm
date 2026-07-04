import { NextRequest, NextResponse } from 'next/server';
import { apiError } from '@/lib/api-error';
import {
  callTransition,
  deliverHumanMessage,
  requireAttendanceAdmin,
  resolveConversation,
  scheduleInactivityTimer,
} from '@/lib/attendance-actions';
import { errorLogFields, log } from '@/lib/logger';
import { validateMessageType } from '@/lib/message-type-validation';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

export const dynamic = 'force-dynamic';

const supabaseAdmin = getSupabaseAdmin();

/**
 * POST /api/admin/conversations/messages — ROTA LEGADA (SEM [id]), §8.1.
 *
 * Consumida pelo chat atual (migra para /[id]/messages em S9/S10). Convertida em
 * SHIM que DELEGA a `record_human_message` (RPC, D1): NENHUMA escrita de mensagem
 * humana pode ignorar a transição/SLA/evento (evita split-brain de mensagem, §24).
 * Mantém o caller funcional: mesmo contrato de body (`conversation_id` no corpo),
 * mesma resposta `{ message }`.
 *
 * Diferença para a versão antiga: (a) valida `type IN ('text','voice')` (§7.1);
 * (b) chama a RPC `record_human_message` ANTES de persistir; (c) grava
 * `author_type='human_operator'`; (d) entrega outbound auditável.
 */
export async function POST(request: NextRequest) {
  try {
    const authResult = await requireAttendanceAdmin(request);
    if (authResult.response) return authResult.response;
    const { auth } = authResult;

    const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
    const conversationId = body.conversation_id;
    const content = typeof body.content === 'string' ? body.content : '';
    const image_url = typeof body.image_url === 'string' ? body.image_url : null;
    const audio_url = typeof body.audio_url === 'string' ? body.audio_url : null;
    const type = body.type === undefined ? 'text' : body.type;

    if (typeof conversationId !== 'string') {
      return apiError('conversation_id is required', { request, status: 400 });
    }

    // Contrato messages.type (§7.1).
    const typeError = validateMessageType(type);
    if (typeError) {
      return apiError(typeError, { request, status: 400 });
    }

    const convResult = await resolveConversation(request, auth, conversationId);
    if (convResult.response) return convResult.response;
    const { conversation } = convResult;

    const cleanContent = content ? content.replace(/^\[👤\s+.+?\]\n/, '') : '';

    // Delega a transição/SLA/evento à RPC ANTES de persistir (D1, §9.1).
    const txResult = await callTransition(request, {
      action: 'record_human_message',
      companyId: auth.companyId,
      conversationId: conversation.id,
      agentId: conversation.agent_id,
      actorType: 'human',
      actorUserId: auth.actorUserId,
      payload: {
        ...auth.actorMetadata,
        type,
        has_image: !!image_url,
        has_audio: !!audio_url,
        legacy_route: true,
      },
    });
    if (txResult.response) return txResult.response;

    const { data: newMessage, error: insertError } = await supabaseAdmin
      .from('messages')
      .insert({
        conversation_id: conversation.id,
        company_id: conversation.company_id,
        role: 'assistant',
        author_type: 'human_operator',
        content: cleanContent,
        image_url: image_url || null,
        audio_url: audio_url || null,
        type: type as string,
        // master_admin não está em users_v2 (FK messages_sender_user_id_fkey);
        // null evita 23503. Identidade do ator vai no evento (actorMetadata na RPC).
        sender_user_id: auth.actorUserId,
      })
      .select()
      .single();

    if (insertError) {
      log.error('[ADMIN MESSAGES] Insert failed', { errorCode: insertError.code });
      return apiError('Failed to save message', { request, status: 500 });
    }

    await supabaseAdmin
      .from('conversations')
      .update({
        last_message_preview: cleanContent.substring(0, 100),
        last_message_at: new Date().toISOString(),
      })
      .eq('id', conversation.id);

    // Hook §8.5 (BLOCKER): esta é a rota LIVE consumida pelo chat ATUAL; ela
    // chama a RPC `record_human_message` direto (sem passar pelo
    // AttendanceService.record_human_message Python, onde vive
    // `on_human_message_persisted`). Espelha /[id]/messages: agenda/reagenda o
    // timer de auto-close após a mensagem humana persistida — senão um operador
    // respondendo pela UI atual NUNCA agenda o auto-close. O backend respeita
    // auto_close_enabled/auto_close_scope. Best-effort: NUNCA derruba a request.
    await scheduleInactivityTimer(request, auth.session, {
      companyId: auth.companyId,
      conversationId: conversation.id,
      agentId: conversation.agent_id,
      attendanceSessionId: txResult.result.attendance_session_id,
    });

    // Entrega outbound auditável + persistência da tentativa em
    // notification_deliveries (§9.1 linhas 1068-1071). Não derruba a request;
    // first_human_response_at já committed pela RPC.
    const deliveryStatus = await deliverHumanMessage(request, {
      conversation,
      session: auth.session,
      attendanceSessionId: txResult.result.attendance_session_id,
      messageId: newMessage.id,
      cleanContent,
      imageUrl: image_url,
      audioUrl: audio_url,
    });

    return NextResponse.json({ message: newMessage, delivery_status: deliveryStatus });
  } catch (error: unknown) {
    log.error('[ADMIN MESSAGES] Error', errorLogFields(error));
    return apiError('Internal server error', {
      cause: error,
      logMessage: '[ADMIN MESSAGES] Request failed',
      request,
      status: 500,
    });
  }
}
