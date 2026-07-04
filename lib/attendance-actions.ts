/**
 * S6 — Helper compartilhado das ações de atendimento (§9.1, D1/§8.1).
 *
 * TODA transição de `conversations.status` a partir do Next passa pela MESMA RPC
 * transacional única `public.rpc_attendance_transition` (D1/§23). É PROIBIDO
 * qualquer `UPDATE` direto em `conversations.status` fora da RPC.
 *
 * Este módulo concentra:
 * - auth via iron-session (admin) e escopo por `company_id` (§17 item 3);
 * - contrato `master_admin`: `company_id` obrigatório como query (§9.1);
 * - resolução/validação de tenancy da conversa;
 * - chamada à RPC via `supabaseAdmin.rpc(...)`;
 * - mapeamento de erros estruturados da RPC (P0001 transição inválida → 400,
 *   P0002 not found → 404, 42501 tenancy → 404, 22023 input inválido → 400).
 *
 * As rotas de AÇÃO (handoff/claim/return-to-ai/close/reopen/messages/...) e os
 * SHIMS legados (§8.1) reusam estas funções para não duplicar a máquina de
 * estados em TypeScript (a RPC é a única fonte de verdade).
 */
import { NextResponse } from 'next/server';
import { getAdminApiKeyOrResponse } from '@/lib/admin-proxy';
import { apiError, authApiError } from '@/lib/api-error';
import { auditCrossTenantAttempt, requireAdminSession } from '@/lib/auth-actions';
import { createInternalAuthHeadersForAdminSession } from '@/lib/internal-jwt';
import type { AdminSessionData } from '@/lib/iron-session';
import { errorLogFields, log } from '@/lib/logger';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

const BACKEND_URL =
  process.env.BACKEND_URL || process.env.NEXT_PUBLIC_BACKEND_URL || 'http://localhost:8000';

const supabaseAdmin = getSupabaseAdmin();

/** Actions válidas da RPC `rpc_attendance_transition` (§6.3). */
export type AttendanceAction =
  | 'request_handoff'
  | 'claim'
  | 'return_to_ai'
  | 'resolve'
  | 'close'
  | 'reopen'
  | 'record_human_message'
  | 'record_customer_message'
  | 'record_ai_message'
  | 'add_note'
  | 'create_event';

/** actor_type aceito pela RPC (CHECK do banco; guard estruturado §7.2/D). */
export type AttendanceActorType = 'customer' | 'agent' | 'human' | 'system';

export type ResolvedConversation = {
  id: string;
  company_id: string;
  agent_id: string | null;
  session_id: string | null;
  status: string;
  channel: string | null;
  user_phone: string | null;
};

type AuthOk = {
  session: AdminSessionData;
  /** company_id efetivo já resolvido para o contrato master_admin (§9.1). */
  companyId: string;
  /**
   * `actor_user_id` SEGURO para passar à RPC. Toda escrita de atendimento faz FK
   * para `users_v2(id)` (conversation_events.actor_user_id, attendance_sessions.*,
   * conversations.assigned_user_id). O `company_admin` tem `adminId` em `users_v2`,
   * mas o `master_admin` tem `adminId` em `admin_users` — SEM relação com
   * `users_v2`. Passar o id do master_admin causaria foreign_key_violation (23503)
   * → 500. Por isso este campo é `null` para master_admin (a identidade do admin
   * fica em `actorMetadata`, gravada no payload do evento). §17 item 3 / §9.1.
   */
  actorUserId: string | null;
  /** Identidade do ator para o payload do evento quando actorUserId é null. */
  actorMetadata: Record<string, unknown>;
};

/**
 * Resolve o `actor_user_id` seguro para a RPC a partir da sessão admin.
 *
 * `company_admin` → `adminId` (é uma linha de `users_v2`). `master_admin` →
 * `null` (seu `adminId` é de `admin_users`, FK-incompatível com `users_v2`); a
 * identidade vai para o payload via `buildActorMetadata`.
 */
export function resolveActorUserId(session: AdminSessionData): string | null {
  return session.role === 'company_admin' ? session.adminId : null;
}

/** Metadados de auditoria do ator admin (para o payload quando user_id é null). */
export function buildActorMetadata(session: AdminSessionData): Record<string, unknown> {
  return {
    actor_admin_id: session.adminId,
    actor_admin_role: session.role,
    actor_admin_email: session.email,
  };
}

/**
 * Autentica o admin e resolve o `company_id` efetivo.
 *
 * - `company_admin`: usa `session.companyId`.
 * - `master_admin`: exige `company_id` como query string (§9.1). Sem ela → 400.
 */
export async function requireAttendanceAdmin(
  request: Request,
): Promise<{ auth: AuthOk; response?: never } | { auth?: never; response: NextResponse }> {
  const result = await requireAdminSession();
  if (result.response) {
    return { response: await authApiError(result.response, { request }) };
  }
  const session = result.session;

  if (session.role === 'company_admin') {
    if (!session.companyId) {
      return { response: apiError('Não autorizado', { request, status: 403 }) };
    }
    return {
      auth: {
        session,
        companyId: session.companyId,
        actorUserId: resolveActorUserId(session),
        actorMetadata: buildActorMetadata(session),
      },
    };
  }

  // master_admin: company_id obrigatório como query (§9.1).
  const url = new URL(request.url);
  const queryCompanyId = url.searchParams.get('company_id') || url.searchParams.get('companyId');
  if (!queryCompanyId) {
    return {
      response: apiError('company_id é obrigatório para master_admin', {
        request,
        status: 400,
      }),
    };
  }
  return {
    auth: {
      session,
      companyId: queryCompanyId,
      actorUserId: resolveActorUserId(session),
      actorMetadata: buildActorMetadata(session),
    },
  };
}

/**
 * Carrega a conversa e valida tenancy contra o `company_id` efetivo do ator.
 * Mismatch → 404 (+ auditoria de cross-tenant para company_admin).
 */
export async function resolveConversation(
  request: Request,
  auth: AuthOk,
  conversationId: string,
): Promise<
  | { conversation: ResolvedConversation; response?: never }
  | { conversation?: never; response: NextResponse }
> {
  const { data, error } = await supabaseAdmin
    .from('conversations')
    .select('id, company_id, agent_id, session_id, status, channel, user_phone')
    .eq('id', conversationId)
    .single();

  if (error || !data) {
    return { response: apiError('Conversa não encontrada', { request, status: 404 }) };
  }

  if (data.company_id !== auth.companyId) {
    if (auth.session.role === 'company_admin') {
      await auditCrossTenantAttempt({
        actorId: auth.session.adminId,
        actorRole: auth.session.role,
        actorCompanyId: auth.session.companyId,
        resourceType: 'conversations',
        resourceId: conversationId,
        targetCompanyId: data.company_id,
        action: 'attendance_action',
        request,
      });
    }
    return { response: apiError('Conversa não encontrada', { request, status: 404 }) };
  }

  return { conversation: data as ResolvedConversation };
}

export type TransitionParams = {
  action: AttendanceAction;
  companyId: string;
  conversationId: string;
  agentId?: string | null;
  actorType?: AttendanceActorType | null;
  actorUserId?: string | null;
  actorAgentId?: string | null;
  payload?: Record<string, unknown>;
  /** Inputs de SLA pré-calculados (handoff/claim). Sem eles ⇒ caminho "none". */
  slaInputs?: SlaInputs | null;
};

/**
 * 4 inputs de SLA + âncora, pré-calculados pelo SlaService (S3) no backend.
 * Sem política ativa, todos os campos vêm `null` (caminho "none", §22 item 5):
 * a RPC então NÃO cria `attendance_sla` e o handoff segue sem SLA.
 */
export type SlaInputs = {
  first_response_deadline: string | null;
  resolution_deadline: string | null;
  sla_level: string | null;
  policy_snapshot: Record<string, unknown> | null;
  started_at: string | null;
};

/**
 * Busca os inputs de SLA via endpoint INTERNO do backend (reusa SlaService — a
 * MESMA matemática 24/7 vs horário útil do webhook/AttendanceService). Isso fecha
 * a divergência (§22 item 5) em que o caminho primário (Next) criava handoff SEM
 * SLA enquanto o caminho legado (webhook) criava COM. Best-effort: qualquer falha
 * devolve "none" (handoff sem SLA), nunca derruba a ação.
 */
export async function fetchSlaInputs(
  request: Request,
  session: AdminSessionData,
  companyId: string,
  conversationId: string,
): Promise<SlaInputs> {
  const none: SlaInputs = {
    first_response_deadline: null,
    resolution_deadline: null,
    sla_level: null,
    policy_snapshot: null,
    started_at: null,
  };
  try {
    const adminApiKey = getAdminApiKeyOrResponse(request);
    if (adminApiKey.response) return none;
    const internalAuthHeaders = createInternalAuthHeadersForAdminSession(session, companyId);
    const response = await fetch(`${BACKEND_URL}/api/internal/attendance/sla-inputs`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Admin-API-Key': adminApiKey.adminApiKey,
        Authorization: internalAuthHeaders.Authorization,
      },
      body: JSON.stringify({ company_id: companyId, conversation_id: conversationId }),
    });
    if (!response.ok) {
      log.warn('[ATTENDANCE sla-inputs] backend returned non-ok', { status: response.status });
      return none;
    }
    const data = (await response.json().catch(() => null)) as Partial<SlaInputs> | null;
    if (!data) return none;
    return {
      first_response_deadline: data.first_response_deadline ?? null,
      resolution_deadline: data.resolution_deadline ?? null,
      sla_level: data.sla_level ?? null,
      policy_snapshot: data.policy_snapshot ?? null,
      started_at: data.started_at ?? null,
    };
  } catch (error: unknown) {
    log.warn('[ATTENDANCE sla-inputs] fetch failed', errorLogFields(error));
    return none;
  }
}

/**
 * Hook §8.5 — agenda o timer de auto-close após uma mensagem humana OUTBOUND.
 *
 * O envio humano canônico é a rota Next POST /[id]/messages, que persiste em
 * `messages` e transiciona via RPC SEM passar pelo `AttendanceService` Python
 * (onde vive o hook `on_human_message_persisted`). Sem este disparo, o timer de
 * auto-close NUNCA nasceria quando um operador responde (quebrando §8.5/§16). O
 * agendamento real (respeitando `auto_close_enabled`/`auto_close_scope`) fica no
 * backend; aqui só fazemos o forward best-effort. NUNCA derruba a request: a
 * mensagem já está persistida e a transição já commitou.
 */
export async function scheduleInactivityTimer(
  request: Request,
  session: AdminSessionData,
  args: {
    companyId: string;
    conversationId: string;
    agentId: string | null;
    attendanceSessionId: string | null;
  },
): Promise<void> {
  try {
    const adminApiKey = getAdminApiKeyOrResponse(request);
    if (adminApiKey.response) return;
    const internalAuthHeaders = createInternalAuthHeadersForAdminSession(session, args.companyId);
    await fetch(`${BACKEND_URL}/api/internal/attendance/timer/schedule`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Admin-API-Key': adminApiKey.adminApiKey,
        Authorization: internalAuthHeaders.Authorization,
      },
      body: JSON.stringify({
        company_id: args.companyId,
        conversation_id: args.conversationId,
        agent_id: args.agentId,
        attendance_session_id: args.attendanceSessionId,
      }),
    });
  } catch (error: unknown) {
    log.warn('[ATTENDANCE timer] schedule forward failed (best-effort)', errorLogFields(error));
  }
}

/**
 * Hook §8.5 — cancela timers pendentes nas transições disparadas pela UI
 * (return-to-ai/close/resolve/reopen/claim). Essas rotas chamam a RPC direto
 * (não passam pelo `AttendanceService._on_attendance_transition`), então o
 * cancel do timer precisa ser disparado aqui — senão um timer agendado pelo
 * caminho IA (`auto_close_scope=all_attendance` em `open`) auto-fecharia uma
 * conversa que acabou de entrar em atendimento humano. Best-effort: a transição
 * já commitou, falha de cancel NUNCA derruba a ação.
 */
export async function cancelInactivityTimer(
  request: Request,
  session: AdminSessionData,
  args: { companyId: string; conversationId: string; transition: string },
): Promise<void> {
  try {
    const adminApiKey = getAdminApiKeyOrResponse(request);
    if (adminApiKey.response) return;
    const internalAuthHeaders = createInternalAuthHeadersForAdminSession(session, args.companyId);
    await fetch(`${BACKEND_URL}/api/internal/attendance/timer/cancel`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Admin-API-Key': adminApiKey.adminApiKey,
        Authorization: internalAuthHeaders.Authorization,
      },
      body: JSON.stringify({
        company_id: args.companyId,
        conversation_id: args.conversationId,
        transition: args.transition,
      }),
    });
  } catch (error: unknown) {
    log.warn('[ATTENDANCE timer] cancel forward failed (best-effort)', errorLogFields(error));
  }
}

export type TransitionResult = {
  status: string;
  previous_status: string;
  conversation_id: string;
  attendance_session_id: string | null;
  attendance_sla_id: string | null;
  event_id: string | null;
};

/**
 * Mapeia ERRCODE da RPC para HTTP. A RPC sinaliza:
 *  - P0001: transição inválida / actor_type inválido → 400.
 *  - P0002: conversa não encontrada → 404.
 *  - 42501: violação de tenancy → 404 (não vazar existência).
 *  - 22023: identificador/action inválida → 400.
 */
function mapRpcError(request: Request, error: { code?: string; message?: string }): NextResponse {
  const code = error.code;
  if (code === 'P0001' || code === '22023') {
    return apiError(error.message || 'Transição de atendimento inválida', {
      request,
      status: 400,
    });
  }
  if (code === 'P0002' || code === '42501') {
    return apiError('Conversa não encontrada', { request, status: 404 });
  }
  // 23503 (foreign_key_violation): ator/destino não existe em users_v2. Não deve
  // ocorrer (actorUserId já é null para master_admin), mas se ocorrer falhamos
  // LIMPO (400) em vez de 500 opaco. Defesa em profundidade do fix de master_admin.
  if (code === '23503') {
    return apiError('Ator inválido para esta ação de atendimento', {
      cause: error,
      logMessage: '[ATTENDANCE] RPC FK violation (actor not in users_v2)',
      request,
      status: 400,
    });
  }
  return apiError('Erro ao processar ação de atendimento', {
    cause: error,
    logMessage: '[ATTENDANCE] RPC transition failed',
    request,
    status: 500,
  });
}

/**
 * Executa a RPC transacional única. ÚNICO ponto de escrita de status no Next.
 */
export async function callTransition(
  request: Request,
  params: TransitionParams,
): Promise<
  { result: TransitionResult; response?: never } | { result?: never; response: NextResponse }
> {
  const sla = params.slaInputs ?? null;
  const rpcArgs: Record<string, unknown> = {
    p_action: params.action,
    p_company_id: params.companyId,
    p_conversation_id: params.conversationId,
    p_session_id: null,
    p_agent_id: params.agentId ?? null,
    p_actor_type: params.actorType ?? null,
    p_actor_user_id: params.actorUserId ?? null,
    p_actor_agent_id: params.actorAgentId ?? null,
    p_payload: params.payload ?? {},
  };
  // Inputs de SLA (handoff/claim): só enviamos quando o SlaService produziu os 4
  // (política ativa). Sem eles, a RPC cai no caminho "none" e não cria
  // attendance_sla (§22 item 5). p_started_at é a âncora ÚNICA dos deadlines.
  if (sla && sla.first_response_deadline && sla.resolution_deadline && sla.sla_level) {
    rpcArgs.p_first_response_deadline = sla.first_response_deadline;
    rpcArgs.p_resolution_deadline = sla.resolution_deadline;
    rpcArgs.p_sla_level = sla.sla_level;
    rpcArgs.p_policy_snapshot = sla.policy_snapshot ?? null;
    if (sla.started_at) rpcArgs.p_started_at = sla.started_at;
  }

  const { data, error } = await supabaseAdmin.rpc('rpc_attendance_transition', rpcArgs);

  if (error) {
    return { response: mapRpcError(request, error as { code?: string; message?: string }) };
  }

  const row = (Array.isArray(data) ? data[0] : data) as TransitionResult | null;
  if (!row) {
    return {
      response: apiError('Erro ao processar ação de atendimento', {
        logMessage: '[ATTENDANCE] RPC returned empty result',
        request,
        status: 500,
      }),
    };
  }
  return { result: row };
}

/**
 * Atalho: auth + resolve conversa + chama a RPC. Usado pelas rotas de ação
 * "puras" (claim/return-to-ai/close/reopen/handoff) que não têm efeitos
 * colaterais além da transição.
 */
export async function runAttendanceAction(
  request: Request,
  conversationId: string,
  build: (ctx: {
    auth: AuthOk;
    conversation: ResolvedConversation;
  }) => TransitionParams | Promise<TransitionParams>,
  options?: {
    /**
     * Hook §8.5: quando definido, cancela timers pendentes APÓS a transição
     * (return-to-ai/close/resolve/reopen/claim). A RPC direta não passa pelo
     * `AttendanceService._on_attendance_transition`, então o cancel é disparado
     * aqui (best-effort). `transition` deriva da action efetiva quando omitido.
     */
    cancelTimerOnTransition?: boolean | ((action: AttendanceAction) => string);
  },
): Promise<NextResponse> {
  const authResult = await requireAttendanceAdmin(request);
  if (authResult.response) return authResult.response;
  const { auth } = authResult;

  const convResult = await resolveConversation(request, auth, conversationId);
  if (convResult.response) return convResult.response;
  const { conversation } = convResult;

  const params = await build({ auth, conversation });
  const txResult = await callTransition(request, params);
  if (txResult.response) return txResult.response;

  // Hook §8.5: cancela o timer de auto-close pendente quando a transição tira a
  // conversa do estado em que o auto-close fazia sentido (best-effort).
  if (options?.cancelTimerOnTransition) {
    const transition =
      typeof options.cancelTimerOnTransition === 'function'
        ? options.cancelTimerOnTransition(params.action)
        : params.action;
    await cancelInactivityTimer(request, auth.session, {
      companyId: params.companyId,
      conversationId: conversation.id,
      transition,
    });
  }

  return NextResponse.json({ success: true, ...txResult.result });
}

export type DeliveryStatus = 'sent' | 'failed' | 'skipped';

export type DeliverHumanMessageParams = {
  conversation: ResolvedConversation;
  session: AdminSessionData;
  /** id da sessão de atendimento corrente, vindo da RPC (pode ser null). */
  attendanceSessionId: string | null;
  /** id da linha em `messages` recém-persistida (idempotência da entrega). */
  messageId: string;
  cleanContent: string;
  imageUrl: string | null;
  audioUrl: string | null;
};

/**
 * Entrega outbound AUDITÁVEL da mensagem humana (§9.1 linhas 1068-1071, §11.4).
 *
 * Reutilizado pelas DUAS rotas `messages` (com e sem `[id]`) para não duplicar a
 * lógica de envio + persistência. Tenta o forward de WhatsApp e SEMPRE persiste o
 * RESULTADO da tentativa em `notification_deliveries` (status sent/failed/skipped +
 * last_error), escopado por `company_id`. Isso dá uma fonte de verdade de entrega
 * para o card/timeline (S7/S9-S10) mostrarem falha e oferecerem retry via
 * `/notifications/resend` (que filtra `notification_deliveries` por conversa).
 *
 * NUNCA derruba a request: a RPC `record_human_message` já commitou
 * `first_human_response_at`; uma falha de provider vira apenas uma linha de
 * delivery `failed`, e a mensagem permanece no histórico (§9.1 linha 1070-1071).
 */
export async function deliverHumanMessage(
  request: Request,
  params: DeliverHumanMessageParams,
): Promise<DeliveryStatus> {
  const { conversation, session, attendanceSessionId, messageId } = params;
  const { cleanContent, imageUrl, audioUrl } = params;

  // Sem WhatsApp ativo (widget/web, ou sem session_id/telefone): nada a entregar.
  if (conversation.channel !== 'whatsapp' || !conversation.session_id || !conversation.user_phone) {
    return 'skipped';
  }

  let deliveryStatus: DeliveryStatus = 'failed';
  let lastError: string | null = null;
  let providerMessageId: string | null = null;

  try {
    const adminApiKey = getAdminApiKeyOrResponse(request);
    if (adminApiKey.response) {
      // Configuração ausente: registra como falha auditável em vez de derrubar.
      lastError = 'admin_api_key_unavailable';
    } else {
      const internalAuthHeaders = createInternalAuthHeadersForAdminSession(
        session,
        conversation.company_id,
      );

      const payload: Record<string, string> = {
        company_id: conversation.company_id,
        session_id: conversation.session_id,
        phone: conversation.user_phone,
      };
      if (imageUrl) payload.image_url = imageUrl;
      else if (audioUrl) payload.audio_url = audioUrl;
      else if (cleanContent) payload.message = cleanContent;

      const response = await fetch(`${BACKEND_URL}/api/webhook/send-message`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Admin-API-Key': adminApiKey.adminApiKey,
          Authorization: internalAuthHeaders.Authorization,
        },
        body: JSON.stringify(payload),
      });

      if (response.ok) {
        deliveryStatus = 'sent';
        const data = (await response.json().catch(() => null)) as {
          message_id?: string;
          provider_message_id?: string;
        } | null;
        providerMessageId = data?.provider_message_id || data?.message_id || null;
      } else {
        deliveryStatus = 'failed';
        lastError = `http_${response.status}`;
        log.warn('[ATTENDANCE messages] WhatsApp delivery failed', {
          status: response.status,
          conversationId: conversation.id,
        });
      }
    }
  } catch (whatsappError: unknown) {
    deliveryStatus = 'failed';
    lastError = whatsappError instanceof Error ? whatsappError.message : 'delivery_error';
    log.error('[ATTENDANCE messages] WhatsApp delivery error', errorLogFields(whatsappError));
  }

  // Persistência AUDITÁVEL da tentativa (§9.1). idempotency_key única por mensagem
  // para não duplicar entre as duas rotas / retries. Best-effort: erro de gravação
  // de auditoria não derruba a request nem desfaz a mensagem já persistida.
  //
  // IMPORTANTE: esta linha usa event_type='human_message' e recipient_value =
  // telefone do CLIENTE. Ela é APENAS auditoria de entrega — o worker de outbox
  // (NotificationService.process_pending) FILTRA por event_type ∈ alertas de
  // handoff e NUNCA seleciona 'human_message'; e /notifications/resend também só
  // reenfileira alertas. Assim o worker JAMAIS re-despacha o template de handoff
  // (com URL admin) para o telefone do cliente (§11.1/§11.4).
  try {
    const nowIso = new Date().toISOString();
    await supabaseAdmin.from('notification_deliveries').insert({
      company_id: conversation.company_id,
      conversation_id: conversation.id,
      attendance_session_id: attendanceSessionId,
      event_type: 'human_message',
      idempotency_key: `human_message:${messageId}`,
      channel: 'whatsapp',
      recipient_value: conversation.user_phone,
      status: deliveryStatus,
      attempts: 1,
      last_attempt_at: nowIso,
      provider_message_id: providerMessageId,
      last_error: lastError,
      sent_at: deliveryStatus === 'sent' ? nowIso : null,
    });
  } catch (persistError: unknown) {
    log.error(
      '[ATTENDANCE messages] Failed to persist delivery attempt',
      errorLogFields(persistError),
    );
  }

  return deliveryStatus;
}

/**
 * Warning estruturado para callers legados de status (§18.1) — usado pelos shims.
 */
export function logLegacyStatusWriteWarning(context: {
  route: string;
  status: string;
  mappedAction: string | null;
  conversationId: string;
  companyId: string;
}): void {
  log.warn('[ATTENDANCE LEGACY] Direct status write routed through RPC shim', {
    route: context.route,
    requestedStatus: context.status,
    mappedAction: context.mappedAction,
    conversationId: context.conversationId,
    companyId: context.companyId,
  });
}
