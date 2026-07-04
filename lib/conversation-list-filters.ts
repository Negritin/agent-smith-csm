/**
 * S10 — Lógica PURA dos filtros/indicadores da LISTA de conversas (SPEC §12.3).
 *
 * Extraída de `app/admin/conversations/page.tsx` para ser testável no runner
 * `node` do vitest (sem DOM/RTL), seguindo o padrão dos demais módulos puros do
 * repo (lib/sla-visual.ts, hooks/use-conversation-polling.ts). A página apenas
 * consome estas funções e mapeia o resultado para JSX.
 *
 * - `quickFilterToServerFilters`: traduz o atalho rápido (§12.3 "Filtros") para os
 *   filtros canônicos enviados a GET /api/admin/conversations (S7), preservando
 *   canal/busca. `pending_customer` cai em `status=human` (refinado no cliente).
 * - `formatDeadline`: tempo restante/decorrido até um prazo de SLA (1ª resposta/
 *   resolução), para os indicadores de tempo da lista.
 * - `slaBadgeKind`: qual badge de SLA mostrar (vencido/risco/nenhum).
 */

import type { ConversationListFilters } from '@/hooks/use-conversation-polling';
import type { SlaHealthStatus } from '@/types/conversation-details';

export type QuickFilter =
  | 'all'
  | 'human'
  | 'mine'
  | 'breached'
  | 'at_risk'
  | 'critical'
  | 'no_sla'
  | 'pending_customer'
  | 'resolved';

export type ChannelFilter = 'all' | 'whatsapp' | 'widget' | 'web';

export type QuickFilterContext = {
  channel: ChannelFilter;
  search: string;
  /** id do operador logado, para o atalho "Meus atendimentos". */
  userId?: string | null;
};

/**
 * Traduz o atalho rápido + canal + busca nos filtros canônicos do servidor.
 * `channel='all'` e busca vazia são omitidos (o backend trata ausência = all).
 */
export function quickFilterToServerFilters(
  quick: QuickFilter,
  ctx: QuickFilterContext,
): ConversationListFilters {
  const f: ConversationListFilters = {};
  if (ctx.channel !== 'all') f.channel = ctx.channel;
  const search = ctx.search.trim();
  if (search) f.search = search;

  switch (quick) {
    case 'human':
      f.status = 'human';
      break;
    case 'resolved':
      f.status = 'resolved';
      break;
    case 'breached':
      f.sla_status = 'breached';
      break;
    case 'at_risk':
      // "SLA em risco" — o servidor (route.ts) trata `at_risk` como at_risk OU
      // critical, casando com o agrupamento visual do badge (slaBadgeKind agrupa
      // at_risk+critical como "SLA risco"). Assim, filtrar risco NÃO esconde
      // conversas críticas que mostram o badge de risco na lista (§12.3).
      f.sla_status = 'at_risk';
      break;
    case 'critical':
      // "SLA crítico" — estrito (75%+ do prazo), para isolar as mais urgentes.
      f.sla_status = 'critical';
      break;
    case 'no_sla':
      // "Sem SLA" — conversas sem política/snapshot de SLA (health_status='none').
      f.sla_status = 'none';
      break;
    case 'pending_customer':
      // PENDING_CUSTOMER está agrupado em `human` no servidor; refinado no cliente.
      f.status = 'human';
      break;
    case 'mine':
      if (ctx.userId) f.assigned_user_id = ctx.userId;
      break;
    case 'all':
    default:
      break;
  }
  return f;
}

export type DeadlineInfo = { text: string; overdue: boolean };

/**
 * Tempo restante (faltam) ou decorrido (vencido, prefixo "+") até um prazo.
 * `null` quando não há prazo configurado/parseável.
 */
export function formatDeadline(
  deadline: string | null | undefined,
  now: number = Date.now(),
): DeadlineInfo | null {
  if (!deadline) return null;
  const target = new Date(deadline).getTime();
  if (Number.isNaN(target)) return null;
  const diffMs = target - now;
  const overdue = diffMs < 0;
  const absMin = Math.round(Math.abs(diffMs) / 60000);
  let value: string;
  if (absMin < 60) value = `${absMin}min`;
  else if (absMin < 1440) value = `${Math.round(absMin / 60)}h`;
  else value = `${Math.round(absMin / 1440)}d`;
  return { text: overdue ? `+${value}` : value, overdue };
}

export type SlaBadgeKind = 'breached' | 'at_risk' | 'critical' | null;

/** Qual badge de SLA a lista deve mostrar para um health_status (§12.3). */
export function slaBadgeKind(health: SlaHealthStatus): SlaBadgeKind {
  if (health === 'breached') return 'breached';
  if (health === 'critical') return 'critical';
  if (health === 'at_risk') return 'at_risk';
  return null;
}

// =========================================================================== //
// Seleção do indicador de TEMPO da lista (1ª resposta vs resolução) — §12.3
// =========================================================================== //

export type ListDeadlinePickInput = {
  health: SlaHealthStatus;
  firstResponseDeadline: string | null | undefined;
  resolutionDeadline: string | null | undefined;
  /** Indício de que já houve resposta humana (1ª resposta cumprida). */
  hasHumanReply: boolean;
};

export type ListDeadlinePick = {
  /** Rótulo do prazo escolhido: até a 1ª resposta OU até a resolução. */
  kind: 'Resposta' | 'Resolução';
  info: DeadlineInfo;
};

/**
 * Decide QUAL prazo o indicador de tempo da lista deve exibir e o formata.
 *
 * Regras (§12.3):
 *  - `health === 'none'` (sem política) ou `'paused'` (relógio parado) ⇒ não há
 *    contagem útil ⇒ retorna `null` (a lista não deve fingir countdown correndo);
 *  - enquanto NÃO houve resposta humana e há prazo de 1ª resposta ⇒ mostra
 *    "Resposta"; caso contrário cai no prazo de "Resolução".
 *
 * Extraída do JSX de `page.tsx` para ser testável no runner `node` (sem DOM).
 */
export function pickListDeadline(
  input: ListDeadlinePickInput,
  now: number = Date.now(),
): ListDeadlinePick | null {
  // Sem política ou SLA pausado ⇒ sem countdown enganoso na lista.
  if (input.health === 'none' || input.health === 'paused') return null;

  const fr = formatDeadline(input.firstResponseDeadline, now);
  const rs = formatDeadline(input.resolutionDeadline, now);
  const useFirstResponse = !input.hasHumanReply && !!fr;
  const info = useFirstResponse ? fr : rs;
  if (!info) return null;
  return { kind: useFirstResponse ? 'Resposta' : 'Resolução', info };
}
