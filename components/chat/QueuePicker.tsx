'use client';

import { Bot, Users, Clock, CheckCircle2, ArrowRight } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

/** Identificador canônico de cada fila — casa com `?status=` da API de conversas. */
export type QueueId = 'agente' | 'humano' | 'nao_respondido' | 'finalizado';

type QueueTone = 'brand' | 'info' | 'warning' | 'success';

export interface QueueDef {
  id: QueueId;
  title: string;
  subtitle: string;
  /** Rótulo do estado (com a "bolinha" colorida) no rodapé do card. */
  stateLabel: string;
  tone: QueueTone;
  icon: LucideIcon;
  /** Valor enviado em `?status=` para o GET /api/admin/conversations. */
  serverStatus: string;
}

/**
 * As 4 filas do inbox. A ordem aqui é a ordem de exibição dos cards.
 * `serverStatus` é consumido tanto pela lista filtrada quanto pelo cabeçalho.
 */
export const QUEUE_DEFS: QueueDef[] = [
  {
    id: 'agente',
    title: 'Atendimento Agente',
    subtitle: 'Conversas conduzidas pela IA',
    stateLabel: 'Com a IA',
    tone: 'brand',
    icon: Bot,
    serverStatus: 'agente',
  },
  {
    id: 'humano',
    title: 'Atendimento Humano',
    subtitle: 'Conversas assumidas por um humano',
    stateLabel: 'Em atendimento',
    tone: 'info',
    icon: Users,
    serverStatus: 'humano',
  },
  {
    id: 'nao_respondido',
    title: 'Atendimento Não Respondido',
    subtitle: 'Aguardando a primeira resposta',
    stateLabel: 'Pendente',
    tone: 'warning',
    icon: Clock,
    serverStatus: 'nao_respondido',
  },
  {
    id: 'finalizado',
    title: 'Atendimento Finalizado',
    subtitle: 'Conversas resolvidas e encerradas',
    stateLabel: 'Concluído',
    tone: 'success',
    icon: CheckCircle2,
    serverStatus: 'finalizado',
  },
];

const TONE: Record<QueueTone, { iconWrap: string; icon: string; dot: string; accent: string }> = {
  brand: {
    iconWrap: 'bg-primary/10',
    icon: 'text-primary',
    dot: 'bg-primary',
    accent: 'text-primary',
  },
  info: { iconWrap: 'bg-info/10', icon: 'text-info', dot: 'bg-info', accent: 'text-info' },
  warning: {
    iconWrap: 'bg-warning/10',
    icon: 'text-warning',
    dot: 'bg-warning',
    accent: 'text-warning',
  },
  success: {
    iconWrap: 'bg-success/10',
    icon: 'text-success',
    dot: 'bg-success',
    accent: 'text-success',
  },
};

export interface QueuePickerProps {
  counts: Partial<Record<QueueId, number>>;
  total: number | null;
  onSelect: (id: QueueId) => void;
  /** Enquanto os contadores ainda não chegaram, mostra "—" no lugar do número. */
  isLoading?: boolean;
}

/**
 * Seletor de filas do inbox (despoluído): em vez de uma lista única misturando
 * tudo, o operador escolhe a fila primeiro. Cada card mostra ícone, contador e o
 * estado da fila; clicar abre a lista filtrada daquela fila.
 */
export function QueuePicker({ counts, total, onSelect, isLoading }: QueuePickerProps) {
  return (
    <div className="flex h-full flex-col">
      {/* Cabeçalho */}
      <div className="flex-shrink-0 border-b border-border p-4">
        <h2 className="text-lg font-bold text-foreground">Atendimentos</h2>
        <p className="text-sm text-muted-foreground">Selecione uma fila para ver as conversas</p>
      </div>

      {/* Cards das filas */}
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
        {QUEUE_DEFS.map((q) => {
          const t = TONE[q.tone];
          const Icon = q.icon;
          const count = counts[q.id];
          const showDash = isLoading && count === undefined;
          return (
            <button
              key={q.id}
              type="button"
              onClick={() => onSelect(q.id)}
              className="group block w-full rounded-xl border border-border bg-card p-4 text-left transition-colors hover:border-primary/40 hover:bg-muted/40"
            >
              <div className="flex items-start justify-between">
                <span
                  className={`flex h-11 w-11 items-center justify-center rounded-xl ${t.iconWrap}`}
                >
                  <Icon className={`h-5 w-5 ${t.icon}`} />
                </span>
                <span className="text-3xl font-bold tabular-nums text-foreground">
                  {showDash ? '—' : (count ?? 0)}
                </span>
              </div>

              <h3 className="mt-3 text-base font-bold text-foreground">{q.title}</h3>
              <p className="text-sm text-muted-foreground">{q.subtitle}</p>

              <div className="mt-3 flex items-center justify-between border-t border-border pt-3">
                <span className="flex items-center gap-1.5 text-sm font-semibold">
                  <span className={`h-2 w-2 rounded-full ${t.dot}`} aria-hidden="true" />
                  <span className={t.accent}>{q.stateLabel}</span>
                </span>
                <span className={`flex items-center gap-1 text-sm font-semibold ${t.accent}`}>
                  Abrir
                  <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
                </span>
              </div>
            </button>
          );
        })}

        {total !== null && (
          <p className="pb-2 pt-1 text-center text-xs text-muted-foreground">
            {total} conversa{total === 1 ? '' : 's'} no total
          </p>
        )}
      </div>
    </div>
  );
}
