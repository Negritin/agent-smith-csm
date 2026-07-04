import { NextRequest } from 'next/server';
import { runAttendanceAction } from '@/lib/attendance-actions';
import { apiError } from '@/lib/api-error';

export const dynamic = 'force-dynamic';

/**
 * POST /api/admin/conversations/[id]/reopen (§9.1, §6.2/§6.3)
 *
 * Reabertura POR ADMIN: RESOLVED|CLOSED -> open, cria NOVA sessão e gera o evento
 * `reopened_by_admin` (actor humano explícito). É DISTINTA da reabertura por
 * mensagem do cliente (`reopened_by_customer`, derivada no gate/inbound de S5/S7):
 * a RPC escolhe o evento pelo `p_actor_type` ('customer' vs humano). Lógica na RPC.
 */
export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return runAttendanceAction(
    request,
    id,
    ({ auth, conversation }) => ({
      action: 'reopen',
      companyId: auth.companyId,
      conversationId: conversation.id,
      agentId: conversation.agent_id,
      // 'human' (não 'customer') => evento reopened_by_admin na RPC.
      actorType: 'human',
      actorUserId: auth.actorUserId,
      payload: { ...auth.actorMetadata },
    }),
    // Hook §8.5: cancela timer órfão da sessão anterior; o reagendamento (quando
    // voltar a aguardar o cliente) é função dos hooks de mensagem.
    { cancelTimerOnTransition: true },
  ).catch((error: unknown) =>
    apiError('Erro interno', {
      cause: error,
      logMessage: '[ATTENDANCE reopen] error',
      request,
      status: 500,
    }),
  );
}
