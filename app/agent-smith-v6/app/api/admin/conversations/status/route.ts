import { NextRequest, NextResponse } from 'next/server';
import { apiError } from '@/lib/api-error';
import {
  callTransition,
  logLegacyStatusWriteWarning,
  requireAttendanceAdmin,
  resolveConversation,
} from '@/lib/attendance-actions';
import { mapStatusToAction } from '@/lib/attendance-status-map';

export const dynamic = 'force-dynamic';

/**
 * PUT /api/admin/conversations/status — SHIM legado (§8.1, D1).
 *
 * Antes fazia UPDATE direto em `conversations.status` (split-brain §24). Agora:
 *  1. valida o status-alvo contra a máquina de estados (§6.3);
 *  2. mapeia status -> ação explícita (request_handoff/claim/return_to_ai/close/reopen);
 *  3. chama a MESMA RPC transacional única — SEM regras próprias de timestamp/SLA/timer;
 *  4. status desconhecido / sem ação mapeada -> 400 (NUNCA grava direto);
 *  5. loga warning estruturado para remoção posterior.
 *
 * DEPRECATED: usar as ações explícitas em /api/admin/conversations/[id]/*.
 */
export async function PUT(request: NextRequest) {
  try {
    const authResult = await requireAttendanceAdmin(request);
    if (authResult.response) return authResult.response;
    const { auth } = authResult;

    const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
    const conversationId = body.conversation_id;
    const status = body.status;
    const reason = body.reason;

    if (typeof conversationId !== 'string' || typeof status !== 'string') {
      return apiError('conversation_id and status are required', { request, status: 400 });
    }

    // Mapeia status-alvo -> ação. Desconhecido / não-acionável -> 400 sem gravar.
    const mapping = mapStatusToAction(status);
    if (!mapping) {
      logLegacyStatusWriteWarning({
        route: 'PUT /api/admin/conversations/status',
        status,
        mappedAction: null,
        conversationId,
        companyId: auth.companyId,
      });
      return apiError(`Status inválido ou não acionável: '${status}'`, { request, status: 400 });
    }

    const convResult = await resolveConversation(request, auth, conversationId);
    if (convResult.response) return convResult.response;
    const { conversation } = convResult;

    logLegacyStatusWriteWarning({
      route: 'PUT /api/admin/conversations/status',
      status,
      mappedAction: mapping.action,
      conversationId,
      companyId: auth.companyId,
    });

    const payload: Record<string, unknown> = { ...auth.actorMetadata };
    if (typeof reason === 'string') payload.reason = reason;

    const txResult = await callTransition(request, {
      action: mapping.action,
      companyId: auth.companyId,
      conversationId: conversation.id,
      agentId: conversation.agent_id,
      actorType: mapping.actorType,
      // null para master_admin (não está em users_v2) — evita 23503.
      actorUserId: auth.actorUserId,
      payload,
    });
    if (txResult.response) return txResult.response;

    return NextResponse.json({ success: true, status: txResult.result.status });
  } catch (error: unknown) {
    return apiError('Internal server error', {
      cause: error,
      logMessage: '[CONV STATUS] Error',
      request,
      status: 500,
    });
  }
}
