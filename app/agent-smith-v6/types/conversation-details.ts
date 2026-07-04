/**
 * S7 — Tipos compartilhados de atendimento/SLA (SPEC §9.1, §6.1, §12.3).
 *
 * FONTE ÚNICA reutilizada pelo frontend (card lateral S9, lista/config S10) e
 * pelas rotas de leitura `GET /api/admin/conversations/[id]/details` e
 * `GET /api/admin/conversations`. O contrato `ConversationDetails` é o de §9.1
 * verbatim; os enums espelham os CHECKs do banco (§6.1, §7.2, §7.5).
 *
 * Conversas antigas sem `attendance_sessions` (§22 itens 4-5): `current_session`
 * é `null` e `sla.health_status` é `'none'` ("Sem SLA configurado").
 */

// =========================================================================== //
// Enums canônicos (espelham os CHECKs do banco)
// =========================================================================== //

/** Status canônicos da conversa (§6.1). `RETURNED_TO_AI` é transitório. */
export type ConversationStatus =
  | 'open'
  | 'HUMAN_REQUESTED'
  | 'HUMAN_ACTIVE'
  | 'PENDING_CUSTOMER'
  | 'RETURNED_TO_AI'
  | 'RESOLVED'
  | 'CLOSED';

/** Estados que agrupam "atendimento humano" no filtro/badge (§6.1, §12.3). */
export const HUMAN_ATTENDANCE_STATUSES: readonly ConversationStatus[] = [
  'HUMAN_REQUESTED',
  'HUMAN_ACTIVE',
  'PENDING_CUSTOMER',
] as const;

/** Status da sessão de atendimento (§7.2). */
export type AttendanceSessionStatus =
  | 'open'
  | 'human_requested'
  | 'human_active'
  | 'pending_customer'
  | 'returned_to_ai'
  | 'resolved'
  | 'closed';

/** Nível de SLA aplicado (§7.5). `null` quando não há política ("none"). */
export type SlaLevel = 'normal' | 'high' | 'critical' | null;

/**
 * Saúde do SLA (§9.1). `'none'` é um estado de LEITURA (não persiste em
 * `attendance_sla.health_status`): representa "sem política ativa" / conversa
 * antiga sem snapshot de SLA (§22 item 5).
 */
export type SlaHealthStatus =
  | 'within_sla'
  | 'at_risk'
  | 'critical'
  | 'breached'
  | 'paused'
  | 'none';

/** Marco de primeira resposta (§7.5). */
export type SlaFirstResponseStatus = 'pending' | 'met' | 'missed';

/** Marco de resolução (§7.5). */
export type SlaResolutionStatus = 'pending' | 'met' | 'missed' | 'breached';

/** Tipos de evento da timeline de atendimento (§7.3). */
export type ConversationEventType =
  | 'attendance_started'
  | 'ai_message_sent'
  | 'customer_message_received'
  | 'handoff_requested'
  | 'handoff_notified'
  | 'human_claimed'
  | 'human_message_sent'
  | 'returned_to_ai'
  | 'resolved_by_human'
  | 'resolved_by_agent'
  | 'closed_by_human'
  | 'closed_by_agent'
  | 'closed_by_system'
  | 'auto_close_scheduled'
  | 'auto_close_cancelled'
  | 'timeout_closed'
  | 'reopened_by_customer'
  | 'reopened_by_admin'
  | 'note_added';

/** Status de entrega de notificação/mensagem (§7.9, §11.4). */
export type NotificationDeliveryStatus =
  | 'pending'
  | 'sent'
  | 'failed'
  | 'skipped'
  | 'cancelled';

/** Status do timer de auto-close (§7.8). */
export type InactivityTimerStatus = 'scheduled' | 'cancelled' | 'executed' | 'failed';

/** Filtros canônicos da lista (§12.3). */
export type ChannelFilter = 'all' | 'whatsapp' | 'widget' | 'web';
export type StatusFilter = 'all' | 'open' | 'human' | 'resolved' | 'closed';
export type SlaStatusFilter = 'all' | 'at_risk' | 'critical' | 'breached' | 'none';

// =========================================================================== //
// Sub-contratos
// =========================================================================== //

/** Resumo de usuário/operador (assignee, lead/user da conversa). */
export type UserSummary = {
  id: string;
  name: string | null;
  email: string | null;
  avatar_url: string | null;
};

/**
 * Resumo da conversa para o card e a lista. Aditivo: superset compatível com o
 * tipo local `Conversation` de `page.tsx` (campos hoje consumidos preservados).
 */
export type ConversationSummary = {
  id: string;
  company_id: string;
  agent_id: string | null;
  session_id: string | null;
  status: ConversationStatus | string;
  channel: string | null;
  user_id: string | null;
  user_name: string | null;
  user_phone: string | null;
  user_email: string | null;
  user_avatar: string | null;
  agent_name: string | null;
  last_message_preview: string | null;
  last_message_at: string | null;
  unread_count: number | null;
  status_color: string | null;
  // Campos de atendimento (§7.1) — null em conversas antigas.
  assigned_user_id: string | null;
  current_attendance_session_id: string | null;
  sla_priority: string | null;
  last_customer_message_at: string | null;
  last_human_message_at: string | null;
  last_ai_message_at: string | null;
  customer_waiting_since: string | null;
  agent_paused: boolean | null;
  created_at: string | null;
};

/** Sessão de atendimento corrente (§7.2). `null` para conversas antigas. */
export type AttendanceSession = {
  id: string;
  conversation_id: string;
  company_id: string;
  agent_id: string | null;
  user_id: string | null;
  channel: string;
  status: AttendanceSessionStatus;
  started_at: string;
  human_requested_at: string | null;
  human_request_reason: string | null;
  human_taken_at: string | null;
  human_taken_by_user_id: string | null;
  first_human_response_at: string | null;
  returned_to_ai_at: string | null;
  resolved_at: string | null;
  closed_at: string | null;
  closed_by_type: 'human' | 'agent' | 'system' | null;
  closed_by_user_id: string | null;
  close_reason: string | null;
  close_summary: string | null;
  created_at: string;
  updated_at: string;
};

/** Snapshot de SLA da sessão (§7.5) projetado no contrato de leitura (§9.1). */
export type SlaSnapshot = {
  health_status: SlaHealthStatus;
  first_response_status: SlaFirstResponseStatus;
  resolution_status: SlaResolutionStatus;
  level: SlaLevel;
  first_response_deadline: string | null;
  resolution_deadline: string | null;
  first_response_at: string | null;
  resolved_at: string | null;
};

/** Evento da timeline de atendimento (§7.3). */
export type ConversationEvent = {
  id: string;
  conversation_id: string;
  attendance_session_id: string | null;
  event_type: ConversationEventType | string;
  actor_type: 'customer' | 'agent' | 'human' | 'system' | null;
  actor_user_id: string | null;
  actor_agent_id: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
};

/** Tentativa de entrega de notificação/mensagem (§7.9, §11.4). */
export type NotificationDelivery = {
  id: string;
  conversation_id: string;
  attendance_session_id: string | null;
  event_type: string;
  channel: string | null;
  recipient_value: string | null;
  status: NotificationDeliveryStatus | string;
  attempts: number | null;
  last_attempt_at: string | null;
  provider_message_id: string | null;
  last_error: string | null;
  sent_at: string | null;
  created_at: string;
};

/** Timer de auto-close ativo (§7.8). */
export type InactivityTimer = {
  id: string;
  conversation_id: string;
  attendance_session_id: string | null;
  timer_type: 'auto_close' | string;
  status: InactivityTimerStatus;
  basis_at: string;
  next_action_at: string;
  executed_at: string | null;
  cancelled_at: string | null;
  error_message: string | null;
  created_at: string;
};

// =========================================================================== //
// Contrato de leitura principal (§9.1)
// =========================================================================== //

/** Resposta de `GET /api/admin/conversations/[id]/details` (§9.1). */
export type ConversationDetails = {
  conversation: ConversationSummary;
  current_session: AttendanceSession | null;
  sla: SlaSnapshot;
  events: ConversationEvent[];
  notification_deliveries: NotificationDelivery[];
  active_timer: InactivityTimer | null;
  assignee: UserSummary | null;
};

/** Item enriquecido da lista `GET /api/admin/conversations` (§9.1 + §12.3). */
export type ConversationListItem = ConversationSummary & {
  /** Saúde de SLA da sessão corrente (`'none'` sem política). */
  sla_health_status: SlaHealthStatus;
  sla_level: SlaLevel;
  sla_first_response_deadline: string | null;
  sla_resolution_deadline: string | null;
  /** Início da sessão de atendimento (âncora das DUAS fases da barra de SLA). */
  sla_started_at: string | null;
  /** Carimbo da 1ª resposta humana — congela a fase 1 da barra. */
  sla_first_response_at: string | null;
  /** `true` quando há timer de auto-close `scheduled`. */
  has_active_timer: boolean;
};

/** Metadados de paginação da lista. */
export type ConversationListPagination = {
  page: number;
  page_size: number;
  total: number;
  has_more: boolean;
};

/** Resposta de `GET /api/admin/conversations` (lista enriquecida). */
export type ConversationListResponse = {
  conversations: ConversationListItem[];
  pagination: ConversationListPagination;
};

// =========================================================================== //
// Classificação de mensagens legadas (§22 item 3) — FONTE ÚNICA
// =========================================================================== //

/** Subconjunto de `messages` necessário para classificar autoria na leitura. */
export type TimelineMessageAuthorFields = {
  role: string | null;
  author_type?: string | null;
  sender_user_id?: string | null;
};

/**
 * Classifica uma mensagem como HUMANA (operador) na leitura (§22 item 3).
 *
 * Mensagens humanas legadas foram persistidas com `role='assistant'` +
 * `sender_user_id != null` (backfill de S1 marca `author_type='human_operator'`).
 * Esta é a regra única que o card/timeline (S9/S10) e qualquer leitura devem
 * usar para NÃO exibir essas mensagens como IA:
 *  - `author_type === 'human_operator'` (caminho novo / backfill), OU
 *  - `role === 'assistant'` com `sender_user_id` (caminho legado pré-backfill).
 *
 * `role='user'` é sempre o CLIENTE (nunca humano-operador); `ai_agent`/`system`
 * são IA/sistema.
 *
 * FRONTEIRA S7 → S9/S10 (explícita): em S7 este helper é a FONTE ÚNICA da
 * classificação, mas a APLICAÇÃO dele numa leitura de timeline de `messages` é
 * de S9/S10. A rota GET /[id]/details do S7 projeta apenas `conversation_events`
 * (auditoria), NÃO a timeline de `messages` — portanto não consome este helper.
 * O endpoint que serve mensagens ao chat (S9/S10, ex.: polling/chat-frame) deve
 * usar ESTE classificador para marcar autoria (idealmente projetando um campo
 * derivado `is_human` no payload), evitando que cada consumidor reimplemente a
 * regra e divirja. Em S7 a classificação está "disponível e testada", não
 * "aplicada numa leitura".
 */
export function messageIsHuman(msg: TimelineMessageAuthorFields): boolean {
  // `role='user'` é SEMPRE o CLIENTE: nunca humano-operador, mesmo que outros
  // campos (author_type/sender_user_id) venham preenchidos por dado inconsistente.
  if (msg.role === 'user') return false;
  if (msg.author_type === 'human_operator') return true;
  return msg.role === 'assistant' && !!msg.sender_user_id;
}
