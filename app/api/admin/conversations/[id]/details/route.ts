import { NextRequest, NextResponse } from 'next/server';
import { apiError } from '@/lib/api-error';
import { requireAttendanceAdmin } from '@/lib/attendance-actions';
import { getSupabaseAdmin } from '@/lib/supabase-admin';
import type {
  AttendanceSession,
  ConversationDetails,
  ConversationEvent,
  ConversationSummary,
  InactivityTimer,
  NotificationDelivery,
  SlaSnapshot,
  UserSummary,
} from '@/types/conversation-details';

export const dynamic = 'force-dynamic';

const supabaseAdmin = getSupabaseAdmin();

/** SLA "none" — sem política ativa / conversa antiga sem snapshot (§22 item 5). */
const SLA_NONE: SlaSnapshot = {
  health_status: 'none',
  first_response_status: 'pending',
  resolution_status: 'pending',
  level: null,
  first_response_deadline: null,
  resolution_deadline: null,
  first_response_at: null,
  resolved_at: null,
};

const EVENTS_LIMIT = 50;
const DELIVERIES_LIMIT = 50;

/**
 * GET /api/admin/conversations/[id]/details (§9.1)
 *
 * Retorna o contrato `ConversationDetails`: conversa, sessão de atendimento
 * corrente, snapshot de SLA (`'none'` sem política), eventos recentes (desc),
 * entregas de notificação, timer ativo e operador responsável.
 *
 * Tolera conversas ANTIGAS sem `attendance_sessions` (§22 itens 4-5):
 * `current_session=null`, `sla.health_status='none'`.
 *
 * Toda leitura é escopada por `company_id` (service_role + iron-session, §17
 * item 3). `master_admin` exige `company_id` na query (§9.1).
 */
export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  try {
    const { id: conversationId } = await params;

    const authResult = await requireAttendanceAdmin(request);
    if (authResult.response) return authResult.response;
    const { auth } = authResult;

    // ===== Conversa (escopada por company_id) =====
    const { data: conv, error: convError } = await supabaseAdmin
      .from('conversations')
      .select(
        `id, company_id, agent_id, session_id, status, channel, user_id, user_name,
         user_phone, user_avatar, agent_name, last_message_preview, last_message_at,
         unread_count, status_color, assigned_user_id, current_attendance_session_id,
         sla_priority, last_customer_message_at, last_human_message_at, last_ai_message_at,
         customer_waiting_since, agent_paused, created_at,
         agents:agent_id (id, name)`,
      )
      .eq('id', conversationId)
      .eq('company_id', auth.companyId)
      .single();

    if (convError || !conv) {
      return apiError('Conversa não encontrada', { request, status: 404 });
    }

    // ===== Lead/user (polimórfico: leads OU users_v2) =====
    const userSummary = conv.user_id ? await resolveUserSummary(conv.user_id as string) : null;

    const conversation: ConversationSummary = {
      id: conv.id,
      company_id: conv.company_id,
      agent_id: conv.agent_id,
      session_id: conv.session_id,
      status: conv.status,
      channel: conv.channel,
      user_id: conv.user_id,
      user_name: conv.user_name || userSummary?.name || null,
      user_phone: conv.user_phone || null,
      user_email: userSummary?.email || null,
      user_avatar: conv.user_avatar || userSummary?.avatar_url || null,
      agent_name: (conv.agents as { name?: string } | null)?.name || conv.agent_name || null,
      last_message_preview: conv.last_message_preview,
      last_message_at: conv.last_message_at,
      unread_count: conv.unread_count ?? null,
      status_color: conv.status_color ?? null,
      assigned_user_id: conv.assigned_user_id ?? null,
      current_attendance_session_id: conv.current_attendance_session_id ?? null,
      sla_priority: conv.sla_priority ?? null,
      last_customer_message_at: conv.last_customer_message_at ?? null,
      last_human_message_at: conv.last_human_message_at ?? null,
      last_ai_message_at: conv.last_ai_message_at ?? null,
      customer_waiting_since: conv.customer_waiting_since ?? null,
      agent_paused: conv.agent_paused ?? null,
      created_at: conv.created_at ?? null,
    };

    // ===== Sessão de atendimento: a CORRENTE ou, se já encerrada, a ÚLTIMA =====
    // O encerramento/return_to_ai zera conversations.current_attendance_session_id na
    // RPC, MAS a sessão e seu attendance_sla continuam no banco (settled). Sem fallback
    // o painel perdia o SLA e o responsável ao encerrar (mostrava "Sem SLA configurado"/
    // "Não atribuído") mesmo havendo histórico. Quando não há ponteiro corrente,
    // buscamos a última sessão da conversa para exibir o desfecho congelado.
    const SESSION_COLS = `id, conversation_id, company_id, agent_id, user_id, channel, status, started_at,
       human_requested_at, human_request_reason, human_taken_at, human_taken_by_user_id,
       first_human_response_at, returned_to_ai_at, resolved_at, closed_at, closed_by_type,
       closed_by_user_id, close_reason, close_summary, created_at, updated_at`;
    const sessionId = conv.current_attendance_session_id as string | null;
    let currentSession: AttendanceSession | null = null;
    if (sessionId) {
      const { data: session } = await supabaseAdmin
        .from('attendance_sessions')
        .select(SESSION_COLS)
        .eq('id', sessionId)
        .eq('company_id', auth.companyId)
        .maybeSingle();
      currentSession = (session as AttendanceSession) ?? null;
    } else {
      // Ponteiro corrente nulo (conversa encerrada/devolvida): última sessão da conversa.
      const { data: lastSession } = await supabaseAdmin
        .from('attendance_sessions')
        .select(SESSION_COLS)
        .eq('conversation_id', conversationId)
        .eq('company_id', auth.companyId)
        .order('started_at', { ascending: false })
        .limit(1)
        .maybeSingle();
      currentSession = (lastSession as AttendanceSession) ?? null;
    }

    // ===== SLA snapshot da sessão EFETIVA (corrente ou última) — 'none' só quando não
    // há sessão/política. Para conversa encerrada, traz o SLA settled (ex.: breached). =====
    const effectiveSessionId = currentSession?.id ?? null;
    let sla: SlaSnapshot = SLA_NONE;
    if (effectiveSessionId) {
      const { data: slaRow } = await supabaseAdmin
        .from('attendance_sla')
        .select(
          `health_status, first_response_status, resolution_status, sla_level,
           first_response_deadline, resolution_deadline, first_response_at, resolved_at`,
        )
        .eq('attendance_session_id', effectiveSessionId)
        .eq('company_id', auth.companyId)
        .maybeSingle();
      if (slaRow) {
        sla = {
          health_status: slaRow.health_status ?? 'within_sla',
          first_response_status: slaRow.first_response_status ?? 'pending',
          resolution_status: slaRow.resolution_status ?? 'pending',
          level: slaRow.sla_level ?? null,
          first_response_deadline: slaRow.first_response_deadline ?? null,
          resolution_deadline: slaRow.resolution_deadline ?? null,
          first_response_at: slaRow.first_response_at ?? null,
          resolved_at: slaRow.resolved_at ?? null,
        };
      }
    }

    // ===== Eventos recentes (desc) =====
    const { data: eventsData } = await supabaseAdmin
      .from('conversation_events')
      .select(
        `id, conversation_id, attendance_session_id, event_type, actor_type,
         actor_user_id, actor_agent_id, metadata, created_at`,
      )
      .eq('conversation_id', conversationId)
      .eq('company_id', auth.companyId)
      .order('created_at', { ascending: false })
      .limit(EVENTS_LIMIT);
    const events = (eventsData as ConversationEvent[]) ?? [];

    // ===== Entregas de notificação (desc) =====
    const { data: deliveriesData } = await supabaseAdmin
      .from('notification_deliveries')
      .select(
        `id, conversation_id, attendance_session_id, event_type, channel, recipient_value,
         status, attempts, last_attempt_at, provider_message_id, last_error, sent_at, created_at`,
      )
      .eq('conversation_id', conversationId)
      .eq('company_id', auth.companyId)
      .order('created_at', { ascending: false })
      .limit(DELIVERIES_LIMIT);
    const notificationDeliveries = (deliveriesData as NotificationDelivery[]) ?? [];

    // ===== Timer ativo (scheduled) =====
    const { data: timerData } = await supabaseAdmin
      .from('conversation_inactivity_timers')
      .select(
        `id, conversation_id, attendance_session_id, timer_type, status, basis_at,
         next_action_at, executed_at, cancelled_at, error_message, created_at`,
      )
      .eq('conversation_id', conversationId)
      .eq('company_id', auth.companyId)
      .eq('status', 'scheduled')
      .order('next_action_at', { ascending: true })
      .limit(1)
      .maybeSingle();
    const activeTimer = (timerData as InactivityTimer) ?? null;

    // ===== Operador responsável (assignee) =====
    // conversations.assigned_user_id é a fonte primária, mas return_to_ai/close pode
    // tê-lo limpado; o histórico de sessão preserva quem atendeu. Fallback: quem assumiu
    // (human_taken_by) ou, na falta, quem fechou (closed_by) a sessão efetiva — assim o
    // "Responsável" não some ao encerrar.
    let assignee: UserSummary | null = null;
    const assigneeId =
      (conv.assigned_user_id as string | null) ??
      currentSession?.human_taken_by_user_id ??
      currentSession?.closed_by_user_id ??
      null;
    if (assigneeId) {
      assignee = await resolveUserSummary(assigneeId, true);
    }

    const body: ConversationDetails = {
      conversation,
      current_session: currentSession,
      sla,
      events,
      notification_deliveries: notificationDeliveries,
      active_timer: activeTimer,
      assignee,
    };

    return NextResponse.json(body);
  } catch (error: unknown) {
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[ADMIN conversation details] error',
      request,
      status: 500,
    });
  }
}

/**
 * Resolve um id polimórfico para `UserSummary`. `usersOnly=true` busca só em
 * `users_v2` (assignee é sempre um operador). Caso geral tenta `leads` e
 * `users_v2` (o `user_id` da conversa pode ser de qualquer um).
 */
async function resolveUserSummary(id: string, usersOnly = false): Promise<UserSummary | null> {
  if (!usersOnly) {
    const { data: lead } = await supabaseAdmin
      .from('leads')
      .select('id, name, email')
      .eq('id', id)
      .maybeSingle();
    if (lead) {
      return { id: lead.id, name: lead.name ?? null, email: lead.email ?? null, avatar_url: null };
    }
  }
  const { data: user } = await supabaseAdmin
    .from('users_v2')
    .select('id, first_name, last_name, email, avatar_url')
    .eq('id', id)
    .maybeSingle();
  if (user) {
    const name = `${user.first_name || ''} ${user.last_name || ''}`.trim() || null;
    return { id: user.id, name, email: user.email ?? null, avatar_url: user.avatar_url ?? null };
  }
  return null;
}
