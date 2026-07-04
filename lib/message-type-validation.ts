/**
 * S6 — Validação do contrato `messages.type` nos endpoints de escrita (§7.1).
 *
 * Domínio canônico: `text | voice`. `voice` é o áudio canônico; IMAGEM continua
 * via `image_url` com `type='text'` (não há tipo `image` no enum — ampliar o
 * domínio está fora do escopo desta SPEC, §7.1 linhas 408-410).
 *
 * Apenas VALIDAR os endpoints — nenhuma migração de enum aqui.
 */
export const ALLOWED_MESSAGE_TYPES = ['text', 'voice'] as const;
export type MessageType = (typeof ALLOWED_MESSAGE_TYPES)[number];

/**
 * Retorna `null` quando o `type` é válido; senão uma mensagem de erro (→ 400).
 * `undefined`/ausente assume default `text`.
 */
export function validateMessageType(type: unknown): string | null {
  if (type === undefined || type === null) {
    return null; // default 'text'
  }
  if (typeof type !== 'string') {
    return "Campo 'type' inválido: esperado 'text' ou 'voice'";
  }
  if (!ALLOWED_MESSAGE_TYPES.includes(type as MessageType)) {
    return `Campo 'type' inválido: '${type}'. Use 'text' ou 'voice' (imagem via image_url com type='text').`;
  }
  return null;
}
