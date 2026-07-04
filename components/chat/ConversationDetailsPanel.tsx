'use client';

/**
 * S9 — Card lateral direito de atendimento (SPEC §12.1/§12.2/§11.4/§6.1/§6.3).
 *
 * `aside` IRMÃO de `ChatMain` (não conteúdo interno do chat). Consome o contrato
 * `ConversationDetails` de `GET /api/admin/conversations/[id]/details` (S7), via
 * o hook de POLLING autenticado (`use-conversation-polling`) — NUNCA via a
 * subscription Supabase Realtime `anon` (removida como dependência em S9, fechada
 * no banco em S11).
 *
 * Blocos (§12.2): cliente, agente, status, responsável, SLA, handoff, timer
 * auto-close (com "previsto"), notificações (status/tentativas/último erro/
 * reenviar — §11.4), timeline (desc) e ações respeitando as transições (§6.3).
 *
 * Ações chamam os endpoints de S6 (claim / return-to-ai / close / sla pause-
 * resume / notifications resend). NÃO usa update direto de status.
 *
 * Estados: loading / erro+retry / vazio. Tolera conversas ANTIGAS sem
 * `attendance_sessions`/SLA (§22 itens 4-5): `current_session=null`,
 * `sla.health_status='none'`.
 */

import * as React from 'react';
import {
  Bell,
  Clock,
  Copy,
  Hand,
  History,
  Mail,
  Phone,
  RotateCcw,
  ShieldCheck,
  User,
} from 'lucide-react';
import { toast } from 'sonner';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { ScrollArea } from '@/components/ui/scroll-area';
import {
  LoadingState,
  ErrorStatePanel,
  EmptyStatePanel,
  RetryButton,
} from '@/components/ui/feedback-state';
import { SlaIndicator } from '@/components/chat/SlaIndicator';
import type {
  ConversationDetails,
  ConversationEvent,
  ConversationStatus,
  NotificationDelivery,
} from '@/types/conversation-details';

// =========================================================================== //
// Labels e máquina de estados (§6.1, §6.3)
// =========================================================================== //

const STATUS_LABELS: Record<string, string> = {
  open: 'Atendimento com IA',
  HUMAN_REQUESTED: 'Aguardando humano',
  HUMAN_ACTIVE: 'Humano ativo',
  PENDING_CUSTOMER: 'Aguardando cliente',
  RETURNED_TO_AI: 'Devolvido para IA',
  RESOLVED: 'Resolvido',
  CLOSED: 'Encerrado',
};

const HUMAN_STATUSES: readonly string[] = ['HUMAN_REQUESTED', 'HUMAN_ACTIVE', 'PENDING_CUSTOMER'];

export function statusLabel(status: string | null | undefined): string {
  if (!status) return '—';
  return STATUS_LABELS[status] ?? status;
}

/**
 * Quais ações são permitidas a partir do status atual (§6.3). Usado para
 * habilitar/desabilitar os botões do card (UX; a RPC valida de fato).
 */
export type AttendanceAction =
  | 'claim'
  | 'return_to_ai'
  | 'resolve'
  | 'close'
  | 'pause_sla'
  | 'resume_sla'
  | 'resend';

export function allowedActions(
  status: string | null | undefined,
  slaPaused: boolean,
): Set<AttendanceAction> {
  const s = (status ?? 'open') as ConversationStatus;
  const actions = new Set<AttendanceAction>();
  // Reenviar alertas e SLA pausa/retoma dependem do SLA, não do status.
  actions.add('resend');
  if (slaPaused) actions.add('resume_sla');
  else actions.add('pause_sla');

  switch (s) {
    case 'open':
      actions.add('claim');
      actions.add('resolve');
      actions.add('close');
      break;
    case 'HUMAN_REQUESTED':
      actions.add('claim');
      actions.add('return_to_ai');
      actions.add('resolve');
      actions.add('close');
      break;
    case 'HUMAN_ACTIVE':
    case 'PENDING_CUSTOMER':
      actions.add('return_to_ai');
      actions.add('resolve');
      actions.add('close');
      break;
    case 'RETURNED_TO_AI':
      actions.add('claim');
      actions.add('resolve');
      actions.add('close');
      break;
    // RESOLVED / CLOSED: terminais — só reenviar alertas / SLA.
    default:
      break;
  }
  return actions;
}

const EVENT_LABELS: Record<string, string> = {
  attendance_started: 'Atendimento iniciado',
  ai_message_sent: 'Mensagem da IA',
  customer_message_received: 'Cliente respondeu',
  handoff_requested: 'Handoff solicitado',
  handoff_notified: 'Alerta de handoff enviado',
  human_claimed: 'Atendimento assumido',
  human_message_sent: 'Mensagem do operador',
  returned_to_ai: 'Devolvido para IA',
  resolved_by_human: 'Resolvido pelo operador',
  resolved_by_agent: 'Resolvido pela IA',
  closed_by_human: 'Encerrado pelo operador',
  closed_by_agent: 'Encerrado pela IA',
  closed_by_system: 'Encerrado pelo sistema',
  auto_close_scheduled: 'Auto-encerramento agendado',
  auto_close_cancelled: 'Auto-encerramento cancelado',
  timeout_closed: 'Encerrado por inatividade',
  reopened_by_customer: 'Reaberto pelo cliente',
  reopened_by_admin: 'Reaberto pelo admin',
  note_added: 'Nota adicionada',
};

function eventLabel(type: string): string {
  return EVENT_LABELS[type] ?? type;
}

const DELIVERY_STATUS_LABELS: Record<string, string> = {
  pending: 'Pendente',
  sent: 'Enviado',
  failed: 'Falhou',
  skipped: 'Ignorado',
  cancelled: 'Cancelado',
};

const DELIVERY_STATUS_TONE: Record<string, string> = {
  pending: 'text-warning',
  sent: 'text-success',
  failed: 'text-danger',
  skipped: 'text-muted-foreground',
  cancelled: 'text-muted-foreground',
};

// =========================================================================== //
// Helpers de formatação
// =========================================================================== //

function fmtDateTime(value: string | null | undefined): string {
  if (!value) return '—';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString('pt-BR', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

async function copyToClipboard(value: string, label: string) {
  try {
    await navigator.clipboard.writeText(value);
    toast.success(`${label} copiado!`);
  } catch {
    toast.error(`Não foi possível copiar ${label.toLowerCase()}.`);
  }
}

/** Iniciais do cliente para o avatar (ex.: "Breno Silva" -> "BS"). */
function initials(name: string | null | undefined): string {
  const n = (name || '').trim();
  if (!n) return 'CL';
  const parts = n.split(/\s+/);
  const first = parts[0]?.[0] ?? '';
  const last = parts.length > 1 ? (parts[parts.length - 1]?.[0] ?? '') : '';
  return (first + last).toUpperCase() || 'CL';
}

// =========================================================================== //
// Componente
// =========================================================================== //

type ConversationDetailsPanelProps = {
  conversationId: string | null;
  details: ConversationDetails | null;
  isLoading: boolean;
  error: string | null;
  /** Refetch do /details (após ação / botão tentar novamente). */
  onRefresh: () => void;
  /** Refetch da lista (após ação que muda status). */
  onListRefresh?: () => void;
  className?: string;
};

export function ConversationDetailsPanel({
  conversationId,
  details,
  isLoading,
  error,
  onRefresh,
  onListRefresh,
  className,
}: ConversationDetailsPanelProps) {
  const [busy, setBusy] = React.useState<AttendanceAction | null>(null);
  // Relógio client-side (tickar tempo decorrido/countdown). Chamado ANTES de qualquer
  // early return para respeitar as regras de hooks.
  const now = useNow();

  // Sem conversa selecionada: o card não deve nem ser montado no desktop
  // (page.tsx controla isso), mas guardamos o estado por segurança.
  if (!conversationId) {
    return (
      <div className={cn('flex h-full flex-col p-4', className)}>
        <EmptyStatePanel
          title="Nenhuma conversa selecionada"
          description="Selecione uma conversa para ver os detalhes do atendimento."
          icon={User}
          className="my-auto"
        />
      </div>
    );
  }

  if (isLoading && !details) {
    return (
      <div className={cn('flex h-full flex-col p-4', className)}>
        <LoadingState label="Carregando atendimento..." className="my-auto" />
      </div>
    );
  }

  if (error && !details) {
    return (
      <div className={cn('flex h-full flex-col p-4', className)}>
        <ErrorStatePanel
          title="Erro ao carregar"
          description={error}
          action={<RetryButton onClick={onRefresh} />}
          className="my-auto"
        />
      </div>
    );
  }

  if (!details) {
    return (
      <div className={cn('flex h-full flex-col p-4', className)}>
        <EmptyStatePanel
          title="Sem detalhes"
          description="Esta conversa ainda não tem dados de atendimento."
          icon={History}
          className="my-auto"
        />
      </div>
    );
  }

  const {
    conversation,
    current_session,
    sla,
    events,
    notification_deliveries,
    active_timer,
    assignee,
  } = details;
  const slaPaused = sla.health_status === 'paused';
  const actions = allowedActions(conversation.status, slaPaused);
  const isHuman = HUMAN_STATUSES.includes(String(conversation.status));

  // ---- Relógio de atendimento (tempo decorrido + countdown de SLA, 100% client-side) ----
  // "Live" = atendimento em curso (sessão sem fim e conversa não-terminal): conta now-início.
  // Encerrado: mostra a duração final (fim-início), estática.
  const isTerminal = conversation.status === 'RESOLVED' || conversation.status === 'CLOSED';
  const sess = current_session;
  const attendanceStartIso =
    sess?.human_taken_at ||
    sess?.human_requested_at ||
    sess?.started_at ||
    conversation.customer_waiting_since ||
    conversation.created_at ||
    null;
  const attendanceEndIso = sess?.closed_at || sess?.resolved_at || sess?.returned_to_ai_at || null;
  const clockLive = !attendanceEndIso && !isTerminal;
  const startMs = attendanceStartIso ? Date.parse(attendanceStartIso) : NaN;
  const elapsedMs = Number.isNaN(startMs)
    ? null
    : (clockLive ? now : attendanceEndIso ? Date.parse(attendanceEndIso) : now) - startMs;
  const hasHumanSession = !!(sess?.human_requested_at || sess?.human_taken_at);
  const clockLabel = !clockLive
    ? 'Duração'
    : hasHumanSession
      ? 'Em atendimento há'
      : 'Conversa ativa há';
  // Countdown de SLA só faz sentido AO VIVO e com prazo ativo (não 'none'/'paused').
  const slaDeadlineIso =
    sla.first_response_status === 'pending' ? sla.first_response_deadline : sla.resolution_deadline;
  const slaRemainingMs =
    clockLive && sla.health_status !== 'none' && sla.health_status !== 'paused' && slaDeadlineIso
      ? Date.parse(slaDeadlineIso) - now
      : null;

  // ---- Executor genérico de ações (endpoints S6) ----
  const runAction = async (
    action: AttendanceAction,
    path: string,
    body: Record<string, unknown> | undefined,
    successMsg: string,
  ) => {
    if (!conversationId) return;
    setBusy(action);
    try {
      const res = await fetch(`/api/admin/conversations/${conversationId}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify(body ?? {}),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error((data as { error?: string }).error || `HTTP ${res.status}`);
      }
      toast.success(successMsg);
      onRefresh();
      onListRefresh?.();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Falha na ação.');
    } finally {
      setBusy(null);
    }
  };

  const phone = conversation.user_phone;
  const email = conversation.user_email;
  const statusTone = isHuman
    ? 'border-0 bg-danger text-primary-foreground'
    : conversation.status === 'RESOLVED'
      ? 'border-0 bg-success/15 text-success'
      : conversation.status === 'CLOSED'
        ? 'border border-border bg-muted text-muted-foreground'
        : 'border-0 bg-primary/15 text-primary';

  return (
    <div className={cn('flex h-full min-h-0 w-full flex-col overflow-hidden bg-card', className)}>
        {/* ===== Cabeçalho: cliente (avatar + nome + status) ===== */}
        <div className="shrink-0 border-b border-border p-4">
          <div className="flex items-center gap-3">
            <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full bg-primary/15 text-sm font-semibold uppercase text-primary">
              {initials(conversation.user_name)}
            </div>
            <div className="min-w-0 flex-1">
              <p className="truncate text-base font-semibold text-foreground">
                {conversation.user_name || 'Cliente'}
              </p>
              <div className="mt-1 flex flex-wrap items-center gap-1.5">
                {conversation.channel && (
                  <Badge className="h-5 border border-border bg-muted px-1.5 text-[9px] font-bold uppercase tracking-wide text-muted-foreground">
                    {conversation.channel}
                  </Badge>
                )}
                <Badge
                  className={cn('h-5 px-1.5 text-[9px] font-bold uppercase tracking-wide', statusTone)}
                >
                  {statusLabel(conversation.status)}
                </Badge>
              </div>
            </div>
          </div>
          <div className="mt-3 space-y-1.5">
            {email && (
              <CopyRow icon={Mail} value={email} onCopy={() => copyToClipboard(email, 'Email')} />
            )}
            {phone && (
              <CopyRow
                icon={Phone}
                value={phone}
                onCopy={() => copyToClipboard(phone, 'Telefone')}
              />
            )}
            <div className="flex items-center gap-1.5 text-xs">
              <User className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              <span className="text-muted-foreground">Agente</span>
              <span className="ml-auto min-w-0 truncate font-medium text-foreground">
                {conversation.agent_name || '—'}
              </span>
            </div>
          </div>
        </div>

        {/* ===== Blocos roláveis ===== */}
        <ScrollArea className="min-h-0 flex-1">
          <div className="space-y-3 p-3">
            {/* ===== Responsável ===== */}
            <Section icon={ShieldCheck} title="Responsável">
              <p className="text-sm text-foreground">
                {assignee?.name || assignee?.email || (
                  <span className="text-muted-foreground">Não atribuído</span>
                )}
              </p>
            </Section>

            {/* ===== SLA ===== */}
            <Section icon={Clock} title="SLA">
              <SlaIndicator sla={sla} variant="full" />
            </Section>

            {/* ===== Tempo (relógio client-side: decorrido + countdown de SLA) ===== */}
            {elapsedMs !== null && (
              <Section icon={Clock} title="Tempo">
                <div className="space-y-1 text-xs">
                  <div className="flex items-baseline justify-between">
                    <span className="text-muted-foreground">{clockLabel}</span>
                    <span className="font-semibold tabular-nums text-foreground">
                      {formatDuration(elapsedMs)}
                    </span>
                  </div>
                  {slaRemainingMs !== null && (
                    <div
                      className="flex items-baseline justify-between"
                      title="Tempo de relógio (horas corridas) até o prazo. O SLA é medido em horário comercial, então o valor pode atravessar fins de semana e madrugadas."
                    >
                      <span className="text-muted-foreground">
                        {slaRemainingMs >= 0 ? 'Prazo de SLA em horas corridas' : 'SLA vencido há'}
                      </span>
                      <span
                        className={cn(
                          'font-semibold tabular-nums',
                          slaRemainingMs >= 0 ? 'text-foreground' : 'text-danger',
                        )}
                      >
                        {formatDuration(Math.abs(slaRemainingMs))}
                      </span>
                    </div>
                  )}
                </div>
              </Section>
            )}

            {/* ===== Handoff ===== */}
            {current_session && (
              <Section icon={Hand} title="Handoff">
                <dl className="space-y-1 text-xs">
                  <Row label="Solicitado" value={fmtDateTime(current_session.human_requested_at)} />
                  <Row label="Assumido" value={fmtDateTime(current_session.human_taken_at)} />
                  <Row
                    label="1ª resposta"
                    value={fmtDateTime(current_session.first_human_response_at)}
                  />
                  {current_session.human_request_reason && (
                    <Row label="Motivo" value={current_session.human_request_reason} />
                  )}
                </dl>
              </Section>
            )}

            {/* ===== Timer de auto-close (previsto) ===== */}
            {active_timer && (
              <Section icon={Clock} title="Auto-encerramento">
                <dl className="space-y-1 text-xs">
                  <Row label="Previsto para" value={fmtDateTime(active_timer.next_action_at)} />
                  <Row label="Base" value={fmtDateTime(active_timer.basis_at)} />
                </dl>
                <p className="mt-1 text-[10px] italic text-muted-foreground">
                  Horário previsto (não exato): depende do tick do worker.
                </p>
              </Section>
            )}

            {/* ===== Notificações (§11.4) ===== */}
            <Section icon={Bell} title="Notificações">
              {notification_deliveries.length === 0 ? (
                <p className="text-xs text-muted-foreground">Sem notificações.</p>
              ) : (
                <ul className="space-y-2">
                  {notification_deliveries.slice(0, 8).map((d) => (
                    <NotificationRow key={d.id} delivery={d} />
                  ))}
                </ul>
              )}
            </Section>

            {/* ===== Timeline (desc) ===== */}
            <Section icon={History} title="Timeline">
              {events.length === 0 ? (
                <p className="text-xs text-muted-foreground">Sem eventos registrados.</p>
              ) : (
                <ol className="space-y-2">
                  {events.slice(0, 12).map((e) => (
                    <TimelineRow key={e.id} event={e} />
                  ))}
                </ol>
              )}
            </Section>
          </div>
        </ScrollArea>

        {/* ===== Ações (rodapé fixo) ===== */}
        <div className="shrink-0 space-y-2 border-t border-border p-3">
        <div className="grid grid-cols-2 gap-2">
          <ActionButton
            label="Assumir"
            icon={Hand}
            disabled={!actions.has('claim') || busy !== null}
            loading={busy === 'claim'}
            onClick={() =>
              runAction(
                'claim',
                '/claim',
                { reason: 'Intervenção manual do admin' },
                'Você assumiu o atendimento.',
              )
            }
          />
          <ActionButton
            label="Devolver para IA"
            icon={RotateCcw}
            disabled={!actions.has('return_to_ai') || busy !== null}
            loading={busy === 'return_to_ai'}
            onClick={() =>
              runAction('return_to_ai', '/return-to-ai', {}, 'Atendimento devolvido para a IA.')
            }
          />
          <ActionButton
            label="Resolver"
            disabled={!actions.has('resolve') || busy !== null}
            loading={busy === 'resolve'}
            onClick={() =>
              runAction(
                'resolve',
                '/close',
                { resolve: true },
                'Atendimento encerrado como resolvido.',
              )
            }
          />
          <ActionButton
            label="Encerrar"
            disabled={!actions.has('close') || busy !== null}
            loading={busy === 'close'}
            onClick={() =>
              runAction('close', '/close', { resolve: false }, 'Atendimento encerrado.')
            }
          />
          {/* Pausar/Retomar SLA removidos da UI: a semântica atual NÃO desconta o tempo
              pausado do prazo (paused_duration_seconds é gravado e nunca lido), então
              pausar apenas mascarava a métrica (chegou a esconder um SLA estourado).
              Reintroduzir só quando o desconto for implementado de fato no SlaService.
              As rotas /sla/pause e /sla/resume seguem existindo, mas sem gatilho na UI. */}
          {/* "Reenviar alertas" removido da UI: o atendente logado É o destinatário
              do alerta (operador = quem recebe), então re-alertar a si mesmo enquanto
              já está na conversa é redundante; e falhas de entrega já são retentadas
              automaticamente pelo worker do outbox. A rota /notifications/resend segue
              existindo, mas sem gatilho na UI. */}
          </div>
        </div>
    </div>
  );
}

// =========================================================================== //
// Subcomponentes
// =========================================================================== //

/** Relógio client-side: re-renderiza a cada `intervalMs` p/ o tempo decorrido tickar. */
function useNow(intervalMs = 1000): number {
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

/** Formata uma duração em ms: "Xh YYm" (>= 1h) ou "Xm YYs". Negativos viram 0. */
function formatDuration(ms: number): string {
  const totalSec = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`;
  return `${m}m ${String(s).padStart(2, '0')}s`;
}

function Section({
  icon: Icon,
  title,
  children,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-border bg-background/40 p-3">
      <h4 className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        <Icon className="h-3.5 w-3.5" />
        {title}
      </h4>
      {children}
    </section>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-start justify-between gap-2">
      <dt className="shrink-0 text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words text-right font-medium text-foreground">{value}</dd>
    </div>
  );
}

function CopyRow({
  icon: Icon,
  value,
  onCopy,
}: {
  icon: React.ComponentType<{ className?: string }>;
  value: string;
  onCopy: () => void;
}) {
  return (
    <div className="flex items-center gap-1.5">
      <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      <span className="min-w-0 flex-1 truncate text-xs text-foreground">{value}</span>
      <button
        type="button"
        onClick={onCopy}
        className="shrink-0 text-muted-foreground hover:text-foreground"
        title="Copiar"
      >
        <Copy className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

function NotificationRow({ delivery }: { delivery: NotificationDelivery }) {
  const statusKey = String(delivery.status);
  return (
    <li className="rounded-md border border-border bg-background/40 px-2 py-1.5 text-xs">
      <div className="flex items-center justify-between gap-2">
        <span className="min-w-0 truncate font-medium text-foreground">
          {delivery.channel || 'notificação'}
        </span>
        <span
          className={cn(
            'shrink-0 font-semibold',
            DELIVERY_STATUS_TONE[statusKey] ?? 'text-muted-foreground',
          )}
        >
          {DELIVERY_STATUS_LABELS[statusKey] ?? statusKey}
        </span>
      </div>
      <div className="mt-0.5 flex items-center justify-between gap-2 text-[10px] text-muted-foreground">
        <span>Tentativas: {delivery.attempts ?? 0}</span>
        {delivery.last_attempt_at && <span>{fmtDateTime(delivery.last_attempt_at)}</span>}
      </div>
      {delivery.last_error && (
        <p className="mt-0.5 break-words text-[10px] text-danger" title={delivery.last_error}>
          {delivery.last_error}
        </p>
      )}
    </li>
  );
}

function TimelineRow({ event }: { event: ConversationEvent }) {
  return (
    <li className="flex gap-2 text-xs">
      <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-primary/60" aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium text-foreground">
          {eventLabel(String(event.event_type))}
        </p>
        <p className="text-[10px] text-muted-foreground">{fmtDateTime(event.created_at)}</p>
      </div>
    </li>
  );
}

function ActionButton({
  label,
  icon: Icon,
  disabled,
  loading,
  onClick,
}: {
  label: string;
  icon?: React.ComponentType<{ className?: string }>;
  disabled?: boolean;
  loading?: boolean;
  onClick: () => void;
}) {
  return (
    <Button
      size="sm"
      variant="outline"
      disabled={disabled}
      onClick={onClick}
      className="w-full min-w-0 justify-center gap-1.5 px-2 text-xs"
      title={label}
    >
      {Icon && <Icon className={cn('h-3.5 w-3.5 shrink-0', loading && 'animate-spin')} />}
      <span className="truncate">{label}</span>
    </Button>
  );
}
