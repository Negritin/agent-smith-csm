import * as React from 'react';

import { cn } from '@/lib/utils';

export interface RankingBarsColumn<T> {
  /** Column header. */
  header: string;
  /** Cell renderer; receives the row. */
  render: (row: T) => React.ReactNode;
  /** Tailwind alignment/width classes for this cell (optional). */
  className?: string;
}

export interface RankingBarsProps<T> {
  /** Rows already sorted (the component does NOT sort). */
  rows: T[];
  /** Primary label per row (the bar's left caption). */
  getLabel: (row: T) => React.ReactNode;
  /** Numeric value that drives the bar width (and the implicit ordering). */
  getValue: (row: T) => number;
  /** Optional secondary metadata columns rendered to the right of the bar. */
  columns?: RankingBarsColumn<T>[];
  /** Stable key per row. */
  getKey: (row: T, index: number) => React.Key;
  /** Suffix appended to the bar value (e.g. "msgs"). */
  valueSuffix?: string;
  className?: string;
}

/**
 * Lista de ranking com barra horizontal por linha. Tom da barra =
 * hsl(var(--primary)). Largura proporcional ao MAIOR valor da lista.
 * Não ordena nada — recebe `rows` já na ordem desejada.
 *
 * Reaproveitado em: Atendimentos (por admin) e Agentes (por agente).
 */
export function RankingBars<T>({
  rows,
  getLabel,
  getValue,
  columns,
  getKey,
  valueSuffix,
  className,
}: RankingBarsProps<T>) {
  const max = rows.reduce((acc, row) => Math.max(acc, getValue(row)), 0);

  return (
    <div className={cn('space-y-3', className)}>
      {columns && columns.length > 0 && (
        <div className="flex items-center gap-4 px-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
          <span className="flex-1">Nome</span>
          {columns.map((col) => (
            <span key={col.header} className={cn('w-20 text-right', col.className)}>
              {col.header}
            </span>
          ))}
        </div>
      )}

      <ul className="space-y-3">
        {rows.map((row, index) => {
          const value = getValue(row);
          const pct = max > 0 ? Math.max(2, (value / max) * 100) : 0;
          return (
            <li key={getKey(row, index)} className="flex items-center gap-4">
              <div className="min-w-0 flex-1">
                <div className="mb-1 flex items-baseline justify-between gap-2">
                  <span className="truncate text-sm font-medium text-foreground">
                    {getLabel(row)}
                  </span>
                  <span className="shrink-0 text-sm font-semibold text-foreground">
                    {new Intl.NumberFormat('pt-BR').format(value)}
                    {valueSuffix ? <span className="ml-1 text-xs text-muted-foreground">{valueSuffix}</span> : null}
                  </span>
                </div>
                <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
                  <div
                    className="h-full rounded-full transition-all"
                    style={{ width: `${pct}%`, backgroundColor: 'hsl(var(--primary))' }}
                  />
                </div>
              </div>

              {columns?.map((col) => (
                <span
                  key={col.header}
                  className={cn('w-20 text-right text-sm text-muted-foreground', col.className)}
                >
                  {col.render(row)}
                </span>
              ))}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
