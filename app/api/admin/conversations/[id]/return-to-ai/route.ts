import { NextRequest } from 'next/server';
import { runAttendanceAction } from '@/lib/attendance-actions';
import { apiError } from '@/lib/api-error';

export const dynamic = 'force-dynamic';

/**
 * POST /api/admin/conversations/[id]/return-to-ai (§9.1, §6.3)
 *
 * Devolve para IA: HUMAN_REQUESTED|HUMAN_ACTIVE -> RETURNED_TO_AI (sessão encerra),
 * estado operacional final = open. Grava evento `returned_to_ai`. NÃO marca
 * resolução. Toda a lógica vive na RPC (D1).
 */
export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return runAttendanceAction(
    request,
    id,
    ({ auth, conversation }) => ({
      action: 'return_to_ai',
      companyId: auth.companyId,
      conversationId: conversation.id,
      agentId: conversation.agent_id,
      actorType: 'human',
      actorUserId: auth.actorUserId,
      payload: { ...auth.actorMetadata },
    }),
    // Hook §8.5: cancela timer de auto-close pendente nesta transição.
    { cancelTimerOnTransition: true },
  ).catch((error: unknown) =>
    apiError('Erro interno', {
      cause: error,
      logMessage: '[ATTENDANCE return-to-ai] error',
      request,
      status: 500,
    }),
  );
}
