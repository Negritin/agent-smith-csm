import { NextRequest } from 'next/server';
import { fetchSlaInputs, runAttendanceAction } from '@/lib/attendance-actions';
import { apiError } from '@/lib/api-error';

export const dynamic = 'force-dynamic';

/**
 * POST /api/admin/conversations/[id]/handoff (§9.1)
 *
 * Solicita handoff: open|RETURNED_TO_AI -> HUMAN_REQUESTED. Como ainda não há
 * dono humano, a RPC pode notificar destinatários (outbox mesmo-commit, §11.1) e
 * INICIA o SLA: os 4 inputs (deadlines/nível/snapshot) + p_started_at são
 * pré-calculados pelo SlaService (via endpoint interno) e passados à RPC, que cria
 * o attendance_sla no MESMO commit. Sem política ativa ⇒ caminho "none" (sem SLA,
 * §22 item 5). Toda a máquina de estados vive na RPC (D1) — nada de update direto.
 */
export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  let body: Record<string, unknown> = {};
  try {
    body = (await request.json()) as Record<string, unknown>;
  } catch {
    body = {};
  }

  return runAttendanceAction(
    request,
    id,
    async ({ auth, conversation }) => {
      const payload: Record<string, unknown> = { ...auth.actorMetadata };
      if (typeof body.reason === 'string') payload.reason = body.reason;
      if (typeof body.summary === 'string') payload.summary = body.summary;
      const slaInputs = await fetchSlaInputs(request, auth.session, auth.companyId, conversation.id);
      return {
        action: 'request_handoff',
        companyId: auth.companyId,
        conversationId: conversation.id,
        agentId: conversation.agent_id,
        // Handoff iniciado por admin é um ator humano (§7.2 aceita agent|human|system).
        actorType: 'human',
        actorUserId: auth.actorUserId,
        payload,
        slaInputs,
      };
    },
    // Hook §8.5: open → HUMAN_REQUESTED não é mais "aguardando o cliente"; cancela
    // um timer de auto-close agendado pelo caminho IA (all_attendance em 'open').
    { cancelTimerOnTransition: true },
  ).catch((error: unknown) =>
    apiError('Erro interno', {
      cause: error,
      logMessage: '[ATTENDANCE handoff] error',
      request,
      status: 500,
    }),
  );
}
