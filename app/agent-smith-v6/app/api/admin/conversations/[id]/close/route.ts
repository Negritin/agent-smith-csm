import { NextRequest } from 'next/server';
import { runAttendanceAction } from '@/lib/attendance-actions';
import { apiError } from '@/lib/api-error';

export const dynamic = 'force-dynamic';

/**
 * POST /api/admin/conversations/[id]/close (§9.1, §6.3)
 *
 * Encerra por humano: -> CLOSED (ou RESOLVED quando `resolve: true` no body).
 * Marca origem do encerramento (closed_by_type=human). Lógica na RPC (D1).
 *
 * Body: { resolve?: boolean, reason?: string, summary?: string }
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
    ({ auth, conversation }) => {
      const payload: Record<string, unknown> = { ...auth.actorMetadata };
      if (typeof body.reason === 'string') payload.reason = body.reason;
      if (typeof body.summary === 'string') payload.summary = body.summary;
      return {
        action: body.resolve === true ? 'resolve' : 'close',
        companyId: auth.companyId,
        conversationId: conversation.id,
        agentId: conversation.agent_id,
        actorType: 'human',
        actorUserId: auth.actorUserId,
        payload,
      };
    },
    // Hook §8.5: close/resolve cancelam timer de auto-close pendente.
    { cancelTimerOnTransition: true },
  ).catch((error: unknown) =>
    apiError('Erro interno', {
      cause: error,
      logMessage: '[ATTENDANCE close] error',
      request,
      status: 500,
    }),
  );
}
