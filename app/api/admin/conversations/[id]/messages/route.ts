import { NextRequest, NextResponse } from 'next/server';
import { apiError } from '@/lib/api-error';
import {
  callTransition,
  deliverHumanMessage,
  requireAttendanceAdmin,
  resolveConversation,
  scheduleInactivityTimer,
} from '@/lib/attendance-actions';
import { log } from '@/lib/logger';
import { validateMessageType } from '@/lib/message-type-validation';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

export const dynamic = 'force-dynamic';

const supabaseAdmin = getSupabaseAdmin();

/**
 * POST /api/admin/conversations/[id]/messages (§9.1, §6.3)
 *
 * Envio de mensagem humana. Fluxo ATÔMICO + AUDITÁVEL (§9.1):
 *  1. valida `type IN ('text','voice')` (imagem via image_url + type='text', §7.1);
 *  2. RPC `record_human_message`: se HUMAN_REQUESTED, assume (HUMAN_ACTIVE),
 *     seta human_taken_at/first_human_response_at, marca SLA 1ª resposta, grava
 *     evento `human_claimed`+`human_message_sent` e vai a PENDING_CUSTOMER —
 *     TUDO numa transação (D1). NENHUM update direto de status aqui;
 *  3. persiste a mensagem (author_type='human_operator', sender_user_id);
 *  4. tenta a entrega outbound (WhatsApp) de forma AUDITÁVEL: falha do provider
 *     NÃO desfaz `first_human_response_at` (a RPC já commitou) — vira só uma
 *     falha de entrega visível, mensagem permanece no histórico.
 */
export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  try {
    const { id } = await params;
    const authResult = await requireAttendanceAdmin(request);
    if (authResult.response) return authResult.response;
    const { auth } = authResult;

    const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
    const content = typeof body.content === 'string' ? body.content : '';
    const image_url = typeof body.image_url === 'string' ? body.image_url : null;
    const audio_url = typeof body.audio_url === 'string' ? body.audio_url : null;
    const type = body.type === undefined ? 'text' : body.type;

    // (C) Contrato messages.type (§7.1): rejeita type fora de (text|voice) -> 400.
    const typeError = validateMessageType(type);
    if (typeError) {
      return apiError(typeError, { request, status: 400 });
    }

    const convResult = await resolveConversation(request, auth, id);
    if (convResult.response) return convResult.response;
    const { conversation } = convResult;

    const cleanContent = content ? content.replace(/^\[👤\s+.+?\]\n/, '') : '';

    // 2. Transição/SLA/evento atômicos via RPC (D1) ANTES de persistir/enviar:
    // assim, mesmo se o envio falhar depois, first_human_response_at fica gravado.
    const txResult = await callTransition(request, {
      action: 'record_human_message',
      companyId: auth.companyId,
      conversationId: conversation.id,
      agentId: conversation.agent_id,
      actorType: 'human',
      actorUserId: auth.actorUserId,
      payload: { ...auth.actorMetadata, type, has_image: !!image_url, has_audio: !!audio_url },
    });
    if (txResult.response) return txResult.response;

    // 3. Persiste a mensagem (autoria canônica §7.1: human_operator).
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
      log.error('[ATTENDANCE messages] Insert failed', { errorCode: insertError.code });
      return apiError('Failed to save message', { request, status: 500 });
    }

    await supabaseAdmin
      .from('conversations')
      .update({
        last_message_preview: cleanContent.substring(0, 100),
        last_message_at: new Date().toISOString(),
      })
      .eq('id', conversation.id);

    // Hook §8.5 (BLOCKER): mensagem humana persistida (outbound aguardando o
    // cliente) → agenda/reagenda o timer de auto-close. Este é o SITE REAL de
    // persistência da mensagem humana; como ele chama a RPC direto (sem passar
    // pelo AttendanceService.record_human_message), o agendamento precisa ser
    // disparado aqui — senão o timer NUNCA nasce quando um operador responde. O
    // backend respeita auto_close_enabled/auto_close_scope. Best-effort.
    await scheduleInactivityTimer(request, auth.session, {
      companyId: auth.companyId,
      conversationId: conversation.id,
      agentId: conversation.agent_id,
      attendanceSessionId: txResult.result.attendance_session_id,
    });

    // 4. Entrega outbound AUDITÁVEL (WhatsApp) + persistência da tentativa em
    // notification_deliveries (§9.1 linhas 1068-1071). Falha NÃO derruba a request
    // nem desfaz first_human_response_at (já commitado pela RPC).
    const deliveryStatus = await deliverHumanMessage(request, {
      conversation,
      session: auth.session,
      attendanceSessionId: txResult.result.attendance_session_id,
      messageId: newMessage.id,
      cleanContent,
      imageUrl: image_url,
      audioUrl: audio_url,
    });

    return NextResponse.json({
      message: newMessage,
      delivery_status: deliveryStatus,
      status: txResult.result.status,
      attendance_session_id: txResult.result.attendance_session_id,
    });
  } catch (error: unknown) {
    return apiError('Internal server error', {
      cause: error,
      logMessage: '[ATTENDANCE messages] Request failed',
      request,
      status: 500,
    });
  }
}
