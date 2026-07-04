'use client';

/**
 * S9 — Indicador visual de SLA (SPEC §12.1/§12.2).
 *
 * Renderiza o status visual `within_sla`/`at_risk`/`critical`/`breached`/
 * `paused`/`none` com indicador de cor + rótulo, mostrando "Sem SLA configurado"
 * quando `none`. A LÓGICA de status vive em `lib/sla-visual.ts` (pura, testada no
 * runner `node` do vitest); aqui só mapeamos `tone` → classes Tailwind.
 *
 * Reutilizado no card lateral (`ConversationDetailsPanel`) e na lista
 * (`page.tsx`) — §12.2.
 */

import * as React from 'react';
import { cn } from '@/lib/utils';
import {
  computeSlaVisual,
  firstResponseLabel,
  resolutionLabel,
  type SlaTone,
} from '@/lib/sla-visual';
import type { SlaProgress } from '@/lib/sla-progress';
import type { SlaSnapshot } from '@/types/conversation-details';

const DOT_BY_TONE: Record<SlaTone, string> = {
  success: 'bg-success',
  warning: 'bg-warning',
  danger: 'bg-danger',
  muted: 'bg-muted-foreground',
  neutral: 'bg-muted-foreground/50',
};

const TEXT_BY_TONE: Record<SlaTone, string> = {
  success: 'text-success',
  warning: 'text-warning',
  danger: 'text-danger',
  muted: 'text-muted-foreground',
  neutral: 'text-muted-foreground',
};

const BADGE_BY_TONE: Record<SlaTone, string> = {
  success: 'bg-success/10 text-success border-success/20',
  warning: 'bg-warning/10 text-warning border-warning/20',
  danger: 'bg-danger/10 text-danger border-danger/20',
  muted: 'bg-muted text-muted-foreground border-border',
  neutral: 'bg-muted/50 text-muted-foreground border-border',
};

const BAR_FILL_BY_TONE: Record<SlaTone, string> = {
  success: 'bg-success',
  warning: 'bg-warning',
  danger: 'bg-danger',
  muted: 'bg-muted-foreground',
  neutral: 'bg-muted-foreground/50',
};

type SlaIndicatorProps = {
  sla: Pick<SlaSnapshot, 'health_status' | 'level'> | null | undefined;
  /**
   * `compact` (lista): só o ponto + rótulo curto.
   * `badge` (lista): pill colorida.
   * `full` (card): bloco com nível, primeira resposta e resolução.
   * `bar` (lista/card): barra de progresso — exige `progress`.
   */
  variant?: 'compact' | 'badge' | 'full' | 'bar';
  /** Progresso pré-computado (`lib/sla-progress.ts`). Obrigatório p/ `variant="bar"`. */
  progress?: SlaProgress;
  className?: string;
};

export function SlaIndicator({ sla, variant = 'compact', progress, className }: SlaIndicatorProps) {
  if (variant === 'bar') {
    if (!progress) return null;
    const pct = Math.round(progress.fraction * 100);
    return (
      <div
        className={cn('h-1.5 w-full overflow-hidden rounded-full bg-muted', className)}
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        title={`SLA ${progress.phase === 'first_response' ? '1ª resposta' : 'resolução'}: ${pct}%`}
      >
        <div
          className={cn('h-full rounded-full transition-[width]', BAR_FILL_BY_TONE[progress.tone])}
          style={{ width: `${pct}%` }}
        />
      </div>
    );
  }

  const visual = computeSlaVisual(sla);

  if (variant === 'badge') {
    return (
      <span
        className={cn(
          'inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide',
          BADGE_BY_TONE[visual.tone],
          className,
        )}
        title={visual.label}
      >
        <span className={cn('h-1.5 w-1.5 rounded-full', DOT_BY_TONE[visual.tone])} aria-hidden />
        <span className="truncate">{visual.label}</span>
      </span>
    );
  }

  if (variant === 'compact') {
    return (
      <span
        className={cn(
          'inline-flex items-center gap-1.5 text-xs',
          TEXT_BY_TONE[visual.tone],
          className,
        )}
        title={visual.label}
      >
        <span
          className={cn('h-2 w-2 shrink-0 rounded-full', DOT_BY_TONE[visual.tone])}
          aria-hidden
        />
        <span className="truncate">{visual.label}</span>
      </span>
    );
  }

  // variant === 'full' (card lateral)
  const snapshot = (sla ?? null) as SlaSnapshot | null;
  return (
    <div className={cn('space-y-2', className)}>
      <div className="flex items-center justify-between gap-2">
        <span
          className={cn(
            'inline-flex items-center gap-1.5 text-sm font-medium',
            TEXT_BY_TONE[visual.tone],
          )}
        >
          <span
            className={cn('h-2.5 w-2.5 shrink-0 rounded-full', DOT_BY_TONE[visual.tone])}
            aria-hidden
          />
          {visual.label}
        </span>
        {visual.levelLabel && (
          <span className="rounded-md border border-border bg-muted/40 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
            {visual.levelLabel}
          </span>
        )}
      </div>

      {!visual.isNone && (
        <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
          <div className="min-w-0">
            <dt className="text-[10px] uppercase tracking-wide text-muted-foreground">
              Primeira resposta
            </dt>
            <dd className="truncate text-foreground">
              {firstResponseLabel(snapshot?.first_response_status)}
            </dd>
          </div>
          <div className="min-w-0">
            <dt className="text-[10px] uppercase tracking-wide text-muted-foreground">Resolução</dt>
            <dd className="truncate text-foreground">
              {resolutionLabel(snapshot?.resolution_status)}
            </dd>
          </div>
        </dl>
      )}
    </div>
  );
}
