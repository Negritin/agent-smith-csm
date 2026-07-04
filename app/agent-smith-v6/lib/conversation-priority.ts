/**
 * S7/S10 — Priorização PURA da LISTA de conversas (SPEC §6.1 / §20 critério 10).
 *
 * Extraída de `app/api/admin/conversations/route.ts` para ser testável no runner
 * `node` do vitest (sem DOM/RTL), seguindo o padrão dos demais módulos puros do
 * repo. A rota apenas enriquece os itens com SLA e usa estas funções para ordenar
 * em memória DENTRO da janela varrida (§MAX_SCAN).
 *
 * Ordem (maior rank = mais no topo):
 *   HUMAN_REQUESTED (4) > SLA em risco/crítico/vencido (3) >
 *   HUMAN_ACTIVE/PENDING_CUSTOMER (2) > demais (1)
 * Desempate: `last_message_at` desc.
 */

import type { SlaHealthStatus } from '@/types/conversation-details';
import { SLA_SEVERITY } from '@/lib/sla-visual';

/** Saúdes de SLA urgentes (em risco/crítico/vencido) para a priorização (§6.1). */
export const SLA_URGENT: ReadonlySet<SlaHealthStatus> = new Set<SlaHealthStatus>([
  'breached',
  'critical',
  'at_risk',
]);

/** Campos mínimos necessários para priorizar (subconjunto de ConversationListItem). */
export interface PrioritizableConversation {
  status: string;
  sla_health_status: SlaHealthStatus;
  last_message_at: string | null;
}

/**
 * Rank de prioridade (§6.1): HUMAN_REQUESTED > SLA vencido/crítico >
 * HUMAN_ACTIVE/PENDING_CUSTOMER > demais. Maior rank = mais no topo.
 */
export function priorityRank(item: PrioritizableConversation): number {
  if (item.status === 'HUMAN_REQUESTED') return 4;
  if (SLA_URGENT.has(item.sla_health_status)) return 3;
  if (item.status === 'HUMAN_ACTIVE' || item.status === 'PENDING_CUSTOMER') return 2;
  return 1;
}

/** Timestamp de `last_message_at` em ms (0 quando ausente) para o desempate. */
export function lastMsg(item: PrioritizableConversation): number {
  return item.last_message_at ? new Date(item.last_message_at).getTime() : 0;
}

/**
 * Comparador para `Array.prototype.sort`: ordena por prioridade desc; em empate de
 * rank usa a severidade de SLA (`breached > critical > at_risk`) e, por fim,
 * `last_message_at` desc. Coloca HUMAN_REQUESTED e SLA em risco/crítico/vencido no
 * topo (`at_risk` AGORA sobe — antes caía no tier base).
 */
export function compareByPriority(
  a: PrioritizableConversation,
  b: PrioritizableConversation,
): number {
  return (
    priorityRank(b) - priorityRank(a) ||
    SLA_SEVERITY[b.sla_health_status] - SLA_SEVERITY[a.sla_health_status] ||
    lastMsg(b) - lastMsg(a)
  );
}
