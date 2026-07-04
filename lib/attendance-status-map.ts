/**
 * S6 — Mapa status → ação para os SHIMS de write sites legados (§8.1).
 *
 * Os endpoints legados que escreviam `conversations.status` direto (PUT
 * /api/admin/conversations/status, PATCH /api/conversations/[id], PATCH webhook
 * /api/conversations/{id}/status) deixam de gravar e passam a:
 *  1. validar o status recebido contra a máquina de estados (§6.3);
 *  2. mapear o status-alvo para a AÇÃO explícita correspondente;
 *  3. chamar a MESMA RPC transacional única.
 *
 * Status desconhecido / sem ação mapeada → 400 (o caller NUNCA grava direto).
 * A validação fina da transição (origem → destino) é feita pela RPC; aqui só
 * resolvemos status-alvo → action. `RETURNED_TO_AI`/`PENDING_CUSTOMER`/`open`
 * via `return_to_ai` (devolução à IA); `PENDING_CUSTOMER` puro é derivado e não
 * é ação manual (§6.3) — o shim aceita o legado `open`/`HUMAN_REQUESTED`/
 * `HUMAN_ACTIVE`/`RESOLVED`/`CLOSED`/`RETURNED_TO_AI`.
 */
import type { AttendanceAction } from '@/lib/attendance-actions';

/** Status canônicos (§6.1) + legados aceitos pelo CHECK. */
const KNOWN_STATUSES = new Set([
  'open',
  'HUMAN_REQUESTED',
  'HUMAN_ACTIVE',
  'PENDING_CUSTOMER',
  'RETURNED_TO_AI',
  'RESOLVED',
  'CLOSED',
]);

export type StatusMapping = {
  action: AttendanceAction;
  /** actor_type explícito da ação legada (sempre operador humano via admin). */
  actorType: 'human';
};

/**
 * Resolve o status-alvo para a ação explícita da máquina de estados (§6.3/§8.1).
 *
 * - `HUMAN_REQUESTED` → `request_handoff` (admin pede humano).
 * - `HUMAN_ACTIVE`    → `claim` (admin assume).
 * - `open` | `RETURNED_TO_AI` → `return_to_ai` (devolve para IA).
 * - `RESOLVED`        → `resolve`.
 * - `CLOSED`          → `close`.
 *
 * `PENDING_CUSTOMER` é DERIVADO (humano envia → pending; cliente responde →
 * active) e NÃO é ação manual de status (§6.3) → retorna null (o shim devolve
 * 400, não grava). Status fora do domínio → null.
 */
export function mapStatusToAction(targetStatus: string): StatusMapping | null {
  if (!KNOWN_STATUSES.has(targetStatus)) {
    return null;
  }
  switch (targetStatus) {
    case 'HUMAN_REQUESTED':
      return { action: 'request_handoff', actorType: 'human' };
    case 'HUMAN_ACTIVE':
      return { action: 'claim', actorType: 'human' };
    case 'open':
    case 'RETURNED_TO_AI':
      return { action: 'return_to_ai', actorType: 'human' };
    case 'RESOLVED':
      return { action: 'resolve', actorType: 'human' };
    case 'CLOSED':
      return { action: 'close', actorType: 'human' };
    // PENDING_CUSTOMER: derivado, não é ação manual de status (§6.3).
    default:
      return null;
  }
}

export function isKnownStatus(status: string): boolean {
  return KNOWN_STATUSES.has(status);
}
