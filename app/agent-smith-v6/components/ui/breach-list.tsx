'use client';

import * as React from 'react';

import { cn } from '@/lib/utils';

export interface SlaBreach {
  conversation_id: string | null;
  customer: string | null;
  admin_name: string | null;
  /** 'first_response' | 'resolution' (derivado do event_type no backend). */
  kind: string;
  deadline: string | null;
  breached_at: string | null;
  delay_minutes: number | null;
}

interface BreachListProps {
  breaches: SlaBreach[];
  /** Quantos itens mostrar antes do "ver mais". */
  initialLimit?: number;
  className?: string;
}

const KIND_LABEL: Record<string, string> = {
  first_response: '1ª resposta',
  first_response_missed: '1ª resposta',
  resolution: 'Resolução',
  resolution_missed: 'Resolução',
  resolution_breached: 'Resolução',
};

function kindLabel(kind: string): string {
  return KIND_LABEL[kind] ?? kind;
}

function formatDateTime(value: string | null): string {
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

function formatDelay(minutes: number | null): string {
  if (minutes == null || Number.isNaN(minutes)) return '—';
  const total = Math.max(0, Math.round(minutes));
  if (total < 60) return `${total}min`;
  const hours = Math.floor(total / 60);
  const mins = total % 60;
  if (hours < 24) return mins > 0 ? `${hours}h${String(mins).padStart(2, '0')}` : `${hours}h`;
  const days = Math.floor(hours / 24);
  const remHours = hours % 24;
  return remHours > 0 ? `${days}d${remHours}h` : `${days}d`;
}

/**
 * Lista read-only de SLAs furados (SPEC §4.2). SEM link/navegação — só exibe.
 * Colunas: Conversa/cliente · Admin responsável · Tipo · Deadline · Estourou em · Atraso.
 * `breaches` já vem ordenado por atraso (desc) e com LIMIT do backend; aqui só
 * aplicamos um "ver mais" client-side por cima do que chegou.
 */
export function BreachList({ breaches, initialLimit = 10, className }: BreachListProps) {
  const [expanded, setExpanded] = React.useState(false);

  const visible = expanded ? breaches : breaches.slice(0, initialLimit);
  const hasMore = breaches.length > initialLimit;

  return (
    <div className={cn('overflow-hidden rounded-xl border border-border bg-card', className)}>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs font-medium uppercase tracking-wide text-muted-foreground">
              <th className="px-4 py-3">Conversa / cliente</th>
              <th className="px-4 py-3">Admin responsável</th>
              <th className="px-4 py-3">Tipo</th>
              <th className="px-4 py-3">Deadline</th>
              <th className="px-4 py-3">Estourou em</th>
              <th className="px-4 py-3 text-right">Atraso</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((b, index) => (
              <tr
                key={`${b.conversation_id ?? 'na'}-${b.kind}-${b.breached_at ?? index}`}
                className="border-b border-border/50 last:border-0"
              >
                <td className="px-4 py-3 text-foreground">
                  {b.customer || b.conversation_id || '—'}
                </td>
                <td className="px-4 py-3 text-muted-foreground">{b.admin_name || '—'}</td>
                <td className="px-4 py-3">
                  <span className="inline-flex items-center rounded-md border border-danger/20 bg-danger/10 px-2 py-0.5 text-xs font-medium text-danger">
                    {kindLabel(b.kind)}
                  </span>
                </td>
                <td className="px-4 py-3 text-muted-foreground">{formatDateTime(b.deadline)}</td>
                <td className="px-4 py-3 text-muted-foreground">{formatDateTime(b.breached_at)}</td>
                <td className="px-4 py-3 text-right font-semibold text-danger">
                  {formatDelay(b.delay_minutes)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {hasMore && (
        <div className="border-t border-border px-4 py-3 text-center">
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="text-sm font-medium text-primary hover:underline"
          >
            {expanded ? 'Ver menos' : `Ver mais (${breaches.length - initialLimit})`}
          </button>
        </div>
      )}
    </div>
  );
}
