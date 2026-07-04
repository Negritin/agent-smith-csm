/**
 * S9 — Lógica PURA do indicador visual de SLA (SPEC §12.1/§12.2).
 *
 * Extraída do componente `SlaIndicator` para ser testável no runner `node` do
 * vitest (sem DOM/RTL), seguindo o padrão dos demais testes de regra pura deste
 * repo (ver `vitest.config.ts`). O componente apenas mapeia o resultado desta
 * função para classes Tailwind/Radix.
 *
 * Fonte única dos status visuais consumidos pelo card lateral E pela lista
 * (§12.2 "reutilizado no card e na lista").
 */

import type {
  SlaFirstResponseStatus,
  SlaHealthStatus,
  SlaLevel,
  SlaResolutionStatus,
  SlaSnapshot,
} from '@/types/conversation-details';

/** Severidade ordenável (maior = mais urgente) — útil p/ priorização/ordenar. */
export const SLA_SEVERITY: Record<SlaHealthStatus, number> = {
  none: 0,
  paused: 1,
  within_sla: 2,
  at_risk: 3,
  critical: 4,
  breached: 5,
};

export type SlaTone = 'neutral' | 'success' | 'warning' | 'danger' | 'muted';

export type SlaVisual = {
  /** Status canônico (§9.1). */
  status: SlaHealthStatus;
  /** Rótulo PT-BR curto para o badge/indicador. */
  label: string;
  /** Tom semântico, mapeado para classes pelo componente. */
  tone: SlaTone;
  /** Nível textual ("Normal"/"Alta"/"Crítica") ou null. */
  levelLabel: string | null;
  /** `true` quando não há política ativa ("Sem SLA configurado"). */
  isNone: boolean;
};

const HEALTH_LABEL: Record<SlaHealthStatus, string> = {
  within_sla: 'Dentro do SLA',
  at_risk: 'SLA em risco',
  critical: 'SLA crítico',
  breached: 'SLA vencido',
  paused: 'SLA pausado',
  none: 'Sem SLA configurado',
};

export const HEALTH_TONE: Record<SlaHealthStatus, SlaTone> = {
  within_sla: 'success',
  at_risk: 'warning',
  critical: 'danger',
  breached: 'danger',
  paused: 'muted',
  none: 'neutral',
};

const LEVEL_LABEL: Record<NonNullable<SlaLevel>, string> = {
  normal: 'Normal',
  high: 'Alta',
  critical: 'Crítica',
};

/** Normaliza um valor possivelmente desconhecido para um `SlaHealthStatus`. */
export function normalizeSlaHealth(value: string | null | undefined): SlaHealthStatus {
  if (value && value in HEALTH_LABEL) return value as SlaHealthStatus;
  return 'none';
}

export function slaLevelLabel(level: SlaLevel): string | null {
  return level ? (LEVEL_LABEL[level] ?? null) : null;
}

/**
 * Calcula o estado visual do SLA a partir do snapshot (§9.1).
 *
 * `health_status='none'` (ou snapshot ausente) ⇒ "Sem SLA configurado" (§12.2),
 * tolerando conversas antigas sem `attendance_sla` (§22 item 5).
 */
export function computeSlaVisual(
  sla: Pick<SlaSnapshot, 'health_status' | 'level'> | null | undefined,
): SlaVisual {
  const status = normalizeSlaHealth(sla?.health_status);
  return {
    status,
    label: HEALTH_LABEL[status],
    tone: HEALTH_TONE[status],
    levelLabel: slaLevelLabel(sla?.level ?? null),
    isNone: status === 'none',
  };
}

const FIRST_RESPONSE_LABEL: Record<SlaFirstResponseStatus, string> = {
  pending: 'Pendente',
  met: 'Cumprida',
  missed: 'Não cumprida',
};

const RESOLUTION_LABEL: Record<SlaResolutionStatus, string> = {
  pending: 'Pendente',
  met: 'Cumprida',
  missed: 'Não cumprida',
  breached: 'Vencida',
};

export function firstResponseLabel(status: SlaFirstResponseStatus | null | undefined): string {
  return status ? (FIRST_RESPONSE_LABEL[status] ?? 'Pendente') : 'Pendente';
}

export function resolutionLabel(status: SlaResolutionStatus | null | undefined): string {
  return status ? (RESOLUTION_LABEL[status] ?? 'Pendente') : 'Pendente';
}
