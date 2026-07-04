import { NextRequest, NextResponse } from 'next/server';
import { apiError } from '@/lib/api-error';
import { requireAttendanceAdmin } from '@/lib/attendance-actions';
import { compareByPriority } from '@/lib/conversation-priority';
import { getSupabaseAdmin } from '@/lib/supabase-admin';
import {
  HUMAN_ATTENDANCE_STATUSES,
  type ConversationListItem,
  type ConversationListResponse,
  type SlaHealthStatus,
  type SlaLevel,
} from '@/types/conversation-details';

export const dynamic = 'force-dynamic';

const supabaseAdmin = getSupabaseAdmin();

const DEFAULT_PAGE_SIZE = 200;
const MAX_PAGE_SIZE = 500;

/**
 * Teto EXPLÍCITO de varredura (substitui o cap implícito ~1000 do PostgREST).
 *
 * A priorização §6.1 mistura `status` (coluna do banco) com `sla_health_status`
 * (enriquecido por uma 2ª query a `attendance_sla`), então não há como ordenar
 * 100% no Postgres sem materializar a prioridade numa coluna (fora do escopo do
 * S7 — exige migration). A invariante portanto é: a priorização §6.1 e o filtro
 * `sla_status` são corretos SOMENTE dentro desta janela varrida. Tornamos o teto
 * explícito (em vez de depender do limite silencioso do PostgREST) para que o
 * comportamento seja determinístico e `total`/`has_more` sejam coerentes com o
 * conjunto efetivamente considerado. Materializar a prioridade para paginar no
 * banco fica como follow-up (ver SPEC §6.1 / §12.3).
 */
const MAX_SCAN = 1000;

/** Estados terminais para o filtro de status (§12.3). */
const RESOLVED_STATUSES = ['RESOLVED'];
const CLOSED_STATUSES = ['CLOSED'];

/**
 * Estados por FILA de atendimento (seletor de filas do inbox). Refinam o
 * agrupamento `human` em duas filas distintas:
 * - "Não Respondido" (Pendente) = aguardando a 1ª resposta = `HUMAN_REQUESTED`.
 * - "Atendimento Humano" (Em atendimento) = humano já atuando = `HUMAN_ACTIVE`
 *   + `PENDING_CUSTOMER` (respondeu, aguardando o cliente).
 * "Finalizado" agrega `RESOLVED` + `CLOSED`. "Agente (IA)" reaproveita `open`.
 */
const HUMAN_ACTIVE_STATUSES = ['HUMAN_ACTIVE', 'PENDING_CUSTOMER'];
const FINALIZED_STATUSES = [...RESOLVED_STATUSES, ...CLOSED_STATUSES];

/**
 * GET /api/admin/conversations (§9.1 + §6.1 + §12.3)
 *
 * Lista enriquecida com paginação, seleção explícita de colunas, filtros
 * canônicos e priorização §6.1. Mantém compat: a resposta sempre carrega a
 * chave `conversations` (consumida por `page.tsx` até S9/S10 migrarem), agora
 * acompanhada de `pagination` e dos campos de SLA/timer por conversa.
 *
 * Filtros canônicos (§12.3):
 * - `channel`: all | whatsapp | widget | web  (widget tratado além de web)
 * - `status`:  all | open | human | resolved | closed  (human agrupa os 3 humanos)
 * - `sla_status`: all | at_risk | critical | breached | none
 * - `agent_id`, `assigned_user_id`, `search` (nome/telefone/preview)
 *
 * Priorização (§6.1): HUMAN_REQUESTED > SLA vencido/crítico >
 * HUMAN_ACTIVE/PENDING_CUSTOMER > demais por `last_message_at` desc.
 *
 * `master_admin`: `company_id` obrigatório na query (§9.1). Toda leitura é
 * escopada por `company_id` (§17 item 3).
 */
export async function GET(request: NextRequest) {
  try {
    const authResult = await requireAttendanceAdmin(request);
    if (authResult.response) return authResult.response;
    const { auth } = authResult;
    const companyId = auth.companyId;

    const url = new URL(request.url);
    const params = url.searchParams;

    const channel = (params.get('channel') || 'all').toLowerCase();
    const statusFilter = (params.get('status') || 'all').toLowerCase();
    const slaStatusFilter = (params.get('sla_status') || 'all').toLowerCase();
    const agentId = params.get('agent_id');
    const assignedUserId = params.get('assigned_user_id');
    // Deep-link "Ver conversas" (F1.5): id do CONTATO (= coluna conversations.user_id),
    // distinto de assigned_user_id (operador). Param da request = contact_user_id.
    const contactUserId = params.get('contact_user_id');
    const search = (params.get('search') || '').trim();

    // Modo LEGADO (não-quebra, §S7): quando o consumidor NÃO envia page/page_size
    // (ex.: app/admin/conversations/page.tsx até S9/S10 adicionarem UI de
    // paginação) devolvemos TODA a janela varrida — sem cortar em 200 — para não
    // encolher silenciosamente a lista/contadores de tenants com >200 conversas.
    // Quando page/page_size É enviado, paginamos de fato.
    const hasPaginationParams = params.has('page') || params.has('page_size');
    const page = Math.max(1, parseInt(params.get('page') || '1', 10) || 1);
    const pageSize = hasPaginationParams
      ? Math.min(
          MAX_PAGE_SIZE,
          Math.max(
            1,
            parseInt(params.get('page_size') || String(DEFAULT_PAGE_SIZE), 10) || DEFAULT_PAGE_SIZE,
          ),
        )
      : MAX_SCAN;

    // ===== Query base (seleção explícita de colunas) =====
    let query = supabaseAdmin
      .from('conversations')
      .select(
        `id, company_id, agent_id, session_id, status, channel, user_id, user_name,
         user_phone, user_avatar, agent_name, last_message_preview, last_message_at,
         unread_count, status_color, assigned_user_id, current_attendance_session_id,
         sla_priority, last_customer_message_at, last_human_message_at, last_ai_message_at,
         customer_waiting_since, agent_paused, created_at,
         agents:agent_id (id, name)`,
        { count: 'exact' },
      )
      .eq('company_id', companyId);

    // ----- Filtro de canal (§12.3: widget tratado além de web) -----
    if (channel === 'whatsapp' || channel === 'widget' || channel === 'web') {
      query = query.eq('channel', channel);
    }

    // ----- Filtro de status canônico (§12.3) + filas do inbox -----
    if (statusFilter === 'human') {
      query = query.in('status', HUMAN_ATTENDANCE_STATUSES as unknown as string[]);
    } else if (statusFilter === 'open' || statusFilter === 'agente') {
      // Fila "Atendimento Agente (IA)" — conversas conduzidas pela IA.
      query = query.eq('status', 'open');
    } else if (statusFilter === 'humano') {
      // Fila "Atendimento Humano" — humano já atuando (sem o HUMAN_REQUESTED).
      query = query.in('status', HUMAN_ACTIVE_STATUSES);
    } else if (statusFilter === 'nao_respondido') {
      // Fila "Não Respondido" — aguardando a 1ª resposta humana.
      query = query.eq('status', 'HUMAN_REQUESTED');
    } else if (statusFilter === 'finalizado') {
      // Fila "Finalizado" — resolvidas + encerradas.
      query = query.in('status', FINALIZED_STATUSES);
    } else if (statusFilter === 'resolved') {
      query = query.in('status', RESOLVED_STATUSES);
    } else if (statusFilter === 'closed') {
      query = query.in('status', CLOSED_STATUSES);
    }

    if (agentId) query = query.eq('agent_id', agentId);
    if (assignedUserId) query = query.eq('assigned_user_id', assignedUserId);
    // Filtro do deep-link de contato: o param é contact_user_id, a COLUNA é user_id.
    // NÃO tocar em assigned_user_id (operador). AND-combinado, aditivo.
    if (contactUserId) query = query.eq('user_id', contactUserId);

    // ----- Busca textual (nome/telefone/preview) -----
    if (search) {
      const safe = search.replace(/[%,()]/g, ' ');
      query = query.or(
        `user_name.ilike.%${safe}%,user_phone.ilike.%${safe}%,last_message_preview.ilike.%${safe}%`,
      );
    }

    // Ordenação base por last_message_at desc (a priorização §6.1 final é aplicada
    // após enriquecer com SLA, em memória, dentro da JANELA varrida). Usa o índice
    // idx_conversations_company_status_last_message (company_id + last_message_at).
    query = query.order('last_message_at', { ascending: false });

    // Teto EXPLÍCITO de varredura (§MAX_SCAN): em vez do cap silencioso ~1000 do
    // PostgREST, limitamos determinística e visivelmente. total/has_more abaixo são
    // tornados coerentes com esta janela (vs. o count exato do banco).
    query = query.range(0, MAX_SCAN - 1);

    const { data: convRows, count, error } = await query;
    if (error) {
      return apiError('Erro ao buscar conversas', {
        cause: error,
        logMessage: '[ADMIN CONVERSATIONS API] query error',
        request,
        status: 500,
      });
    }

    const rows = convRows ?? [];

    // ===== Enriquecimento de SLA por sessão corrente =====
    const sessionIds = Array.from(
      new Set(rows.map((c) => c.current_attendance_session_id).filter(Boolean)),
    ) as string[];

    const slaBySession = new Map<
      string,
      {
        health: SlaHealthStatus;
        level: SlaLevel;
        frd: string | null;
        rd: string | null;
        fra: string | null;
      }
    >();
    // started_at vem de attendance_sessions (a lista nunca consultava essa tabela).
    const startedBySession = new Map<string, string | null>();
    // Timers scheduled correlacionados por conversation_id (NÃO por session_id): o
    // timer agendado pelo caminho IA (auto_close_scope=all_attendance em 'open')
    // tem attendance_session_id NULL, então um filtro por session perderia o
    // indicador. Correlacionar por conversa cobre IA + humano (§12.3 ícone timer).
    const timersByConversation = new Set<string>();

    if (sessionIds.length > 0) {
      const [{ data: slaRows }, { data: sessionRows }] = await Promise.all([
        supabaseAdmin
          .from('attendance_sla')
          .select(
            'attendance_session_id, health_status, sla_level, first_response_deadline, resolution_deadline, first_response_at',
          )
          .eq('company_id', companyId)
          .in('attendance_session_id', sessionIds),
        supabaseAdmin
          .from('attendance_sessions')
          .select('id, started_at')
          .eq('company_id', companyId)
          .in('id', sessionIds),
      ]);
      for (const s of slaRows ?? []) {
        slaBySession.set(s.attendance_session_id, {
          health: (s.health_status as SlaHealthStatus) ?? 'within_sla',
          level: (s.sla_level as SlaLevel) ?? null,
          frd: s.first_response_deadline ?? null,
          rd: s.resolution_deadline ?? null,
          fra: s.first_response_at ?? null,
        });
      }
      for (const sess of sessionRows ?? []) {
        startedBySession.set(sess.id, sess.started_at ?? null);
      }
    }

    // Timers ativos por conversa (cobre IA com session NULL + humano).
    const conversationIds = rows.map((c) => c.id).filter(Boolean) as string[];
    if (conversationIds.length > 0) {
      const { data: timerRows } = await supabaseAdmin
        .from('conversation_inactivity_timers')
        .select('conversation_id')
        .eq('company_id', companyId)
        .eq('status', 'scheduled')
        .in('conversation_id', conversationIds);
      for (const t of timerRows ?? []) {
        if (t.conversation_id) timersByConversation.add(t.conversation_id);
      }
    }

    // ===== Lead/user lookup (polimórfico) =====
    const userIds = Array.from(new Set(rows.map((c) => c.user_id).filter(Boolean))) as string[];
    const leadMap = new Map<string, { name?: string; email?: string }>();
    const userMap = new Map<
      string,
      { first_name?: string; last_name?: string; email?: string; avatar_url?: string }
    >();
    if (userIds.length > 0) {
      const [{ data: leadsData }, { data: usersData }] = await Promise.all([
        supabaseAdmin.from('leads').select('id, name, email').in('id', userIds),
        supabaseAdmin
          .from('users_v2')
          .select('id, first_name, last_name, email, avatar_url')
          .in('id', userIds),
      ]);
      for (const l of leadsData ?? []) leadMap.set(l.id, l);
      for (const u of usersData ?? []) userMap.set(u.id, u);
    }

    // ===== Monta os itens enriquecidos =====
    const items: ConversationListItem[] = rows.map((conv) => {
      const sessionId = conv.current_attendance_session_id as string | null;
      const slaInfo = sessionId ? slaBySession.get(sessionId) : undefined;
      const lead = conv.user_id ? leadMap.get(conv.user_id) : undefined;
      const user = conv.user_id ? userMap.get(conv.user_id) : undefined;
      const profileName = user
        ? `${user.first_name || ''} ${user.last_name || ''}`.trim() || null
        : null;

      return {
        id: conv.id,
        company_id: conv.company_id,
        agent_id: conv.agent_id,
        session_id: conv.session_id,
        status: conv.status,
        channel: conv.channel,
        user_id: conv.user_id,
        user_name: conv.user_name || lead?.name || profileName || null,
        user_phone: conv.user_phone || null,
        user_email: lead?.email || user?.email || null,
        user_avatar: conv.user_avatar || user?.avatar_url || null,
        agent_name: (conv.agents as { name?: string } | null)?.name || conv.agent_name || null,
        last_message_preview: conv.last_message_preview,
        last_message_at: conv.last_message_at,
        unread_count: conv.unread_count ?? null,
        status_color: conv.status_color ?? null,
        assigned_user_id: conv.assigned_user_id ?? null,
        current_attendance_session_id: sessionId,
        sla_priority: conv.sla_priority ?? null,
        last_customer_message_at: conv.last_customer_message_at ?? null,
        last_human_message_at: conv.last_human_message_at ?? null,
        last_ai_message_at: conv.last_ai_message_at ?? null,
        customer_waiting_since: conv.customer_waiting_since ?? null,
        agent_paused: conv.agent_paused ?? null,
        created_at: conv.created_at ?? null,
        sla_health_status: slaInfo?.health ?? 'none',
        sla_level: slaInfo?.level ?? null,
        sla_first_response_deadline: slaInfo?.frd ?? null,
        sla_resolution_deadline: slaInfo?.rd ?? null,
        sla_started_at: sessionId ? (startedBySession.get(sessionId) ?? null) : null,
        sla_first_response_at: slaInfo?.fra ?? null,
        has_active_timer: timersByConversation.has(conv.id),
      };
    });

    // ----- Filtro de SLA (depende do enriquecimento; aplicado em memória) -----
    //
    // `at_risk` engloba `critical` para casar com o agrupamento VISUAL do badge
    // "SLA risco" na lista (slaBadgeKind agrupa at_risk+critical, §12.3): filtrar
    // "SLA em risco" NÃO pode esconder conversas críticas que ostentam o badge de
    // risco. O atalho `critical` permanece ESTRITO para quem quiser só as mais
    // urgentes (75%+ do prazo).
    let filtered = items;
    if (slaStatusFilter !== 'all') {
      filtered = items.filter((it) => {
        if (slaStatusFilter === 'at_risk')
          return it.sla_health_status === 'at_risk' || it.sla_health_status === 'critical';
        if (slaStatusFilter === 'critical') return it.sla_health_status === 'critical';
        if (slaStatusFilter === 'breached') return it.sla_health_status === 'breached';
        if (slaStatusFilter === 'none') return it.sla_health_status === 'none';
        return true;
      });
    }

    // ===== Priorização §6.1 (comparador PURO em lib/conversation-priority) =====
    filtered.sort(compareByPriority);

    // ===== Paginação (após filtro/ordenação em memória, dentro da janela) =====
    // `total`/`has_more` coerentes com o conjunto EFETIVAMENTE considerado:
    //  - sem filtro de SLA e sem truncamento da janela ⇒ usa o count exato do banco
    //    (caso comum em tenants pequenos: total reflete o banco inteiro);
    //  - com filtro de SLA OU quando a janela truncou (>MAX_SCAN) ⇒ usa
    //    filtered.length, pois o count exato divergiria do que é paginável aqui.
    const scanTruncated = (count ?? rows.length) > rows.length;
    const total =
      slaStatusFilter === 'all' && !scanTruncated ? (count ?? filtered.length) : filtered.length;
    const start = (page - 1) * pageSize;
    const pageItems = filtered.slice(start, start + pageSize);

    const body: ConversationListResponse = {
      conversations: pageItems,
      pagination: {
        page,
        page_size: pageSize,
        total,
        // has_more deriva da MESMA base de filtered (não do count do banco), para
        // não prometer páginas além do conjunto realmente paginável nesta janela.
        has_more: start + pageItems.length < filtered.length,
      },
    };
    return NextResponse.json(body);
  } catch (error: unknown) {
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[ADMIN CONVERSATIONS API] error',
      request,
      status: 500,
    });
  }
}
