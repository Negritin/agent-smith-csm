import { NextRequest } from 'next/server';
import { fetchSlaInputs, runAttendanceAction } from '@/lib/attendance-actions';
import { apiError } from '@/lib/api-error';

export const dynamic = 'force-dynamic';

/**
 * POST /api/admin/conversations/[id]/claim (§9.1, §6.3)
 *
 * Tomada manual: open|HUMAN_REQUESTED -> HUMAN_ACTIVE em UMA transação.
 * NÃO notifica (a RPC garante isso — só `request_handoff` enfileira deliveries).
 * Quando a tomada parte de `open` (sem handoff prévio), a RPC inicia o SLA: os 4
 * inputs + p_started_at são pré-calculados pelo SlaService (endpoint interno),
 * igual ao caminho legado do webhook. Sem política ⇒ "none" (§22 item 5). A 1ª
 * resposta de SLA é marcada como cumprida pelo próprio ato de assumir.
 */
export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return runAttendanceAction(
    request,
    id,
    async ({ auth, conversation }) => ({
      action: 'claim',
      companyId: auth.companyId,
      conversationId: conversation.id,
      agentId: conversation.agent_id,
      actorType: 'human',
      actorUserId: auth.actorUserId,
      payload: { ...auth.actorMetadata },
      slaInputs: await fetchSlaInputs(request, auth.session, auth.companyId, conversation.id),
    }),
    // Hook §8.5: ao assumir (open/HUMAN_REQUESTED → HUMAN_ACTIVE) cancela um timer
    // de auto-close agendado pelo caminho IA (auto_close_scope=all_attendance em
    // 'open'); senão a conversa recém-assumida poderia auto-fechar.
    { cancelTimerOnTransition: true },
  ).catch((error: unknown) =>
    apiError('Erro interno', {
      cause: error,
      logMessage: '[ATTENDANCE claim] error',
      request,
      status: 500,
    }),
  );
}
