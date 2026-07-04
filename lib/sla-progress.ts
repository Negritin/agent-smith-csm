/**
 * S6 — Lógica PURA da barra de progresso de SLA (SPEC §3.2).
 *
 * `started_at` é a âncora das DUAS fases. Fase "primeira resposta" usa
 * `first_response_deadline` e CONGELA quando `first_response_at` é setado;
 * fase "resolução" usa `resolution_deadline`. Cor vem da mesma `SlaTone` de
 * `lib/sla-visual.ts` (verde/amarelo/vermelho). Testável no runner `node`.
 */
import type { SlaHealthStatus } from '@/types/conversation-details';
import { HEALTH_TONE, type SlaTone } from '@/lib/sla-visual';

export type SlaProgressPhase = 'first_response' | 'resolution';

export type SlaProgress = {
  /** Fração preenchida [0,1]. */
  fraction: number;
  /** Tom semântico (mapeado para classes pelo componente). */
  tone: SlaTone;
  /** Fase corrente da barra. */
  phase: SlaProgressPhase;
  /** `true` quando ultrapassou o deadline (fração saturada em 1). */
  overdue: boolean;
} | null;

export type SlaProgressInput = {
  health: SlaHealthStatus;
  /** attendance_sessions.started_at — âncora de AMBAS as fases. */
  startedAt: string | null;
  firstResponseDeadline: string | null;
  /** congela a fase 1 quando presente. */
  firstResponseAt: string | null;
  resolutionDeadline: string | null;
  /** epoch ms; default Date.now() — injetar nos testes p/ determinismo. */
  now?: number;
};

const clamp01 = (n: number): number => (n < 0 ? 0 : n > 1 ? 1 : n);

function ms(value: string | null): number | null {
  if (!value) return null;
  const t = new Date(value).getTime();
  return Number.isFinite(t) ? t : null;
}

export function computeSlaProgress(input: SlaProgressInput): SlaProgress {
  const { health } = input;
  // Sem política / pausado ⇒ sem barra.
  if (health === 'none' || health === 'paused') return null;

  const start = ms(input.startedAt);
  if (start === null) return null;

  const frDeadline = ms(input.firstResponseDeadline);
  const resDeadline = ms(input.resolutionDeadline);
  const firstResponseAt = ms(input.firstResponseAt);
  const now = input.now ?? Date.now();

  // Fase 1 enquanto não houve 1ª resposta E existe deadline de 1ª resposta.
  const inFirstResponse = firstResponseAt === null && frDeadline !== null;
  const phase: SlaProgressPhase = inFirstResponse ? 'first_response' : 'resolution';
  const deadline = inFirstResponse ? frDeadline : resDeadline;

  if (deadline === null) return null;
  const span = deadline - start;
  if (span <= 0) return null; // zero/negativo ⇒ sem barra (guarda divisão).

  // Fase 1 congela no instante da 1ª resposta (se já houve); senão usa `now`.
  // Fase 2 sempre usa `now`.
  const reference = phase === 'first_response' && firstResponseAt !== null ? firstResponseAt : now;

  const raw = (reference - start) / span;
  const fraction = clamp01(raw);

  return {
    fraction,
    tone: HEALTH_TONE[health],
    phase,
    overdue: raw >= 1,
  };
}
