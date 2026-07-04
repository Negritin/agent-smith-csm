/**
 * S6 — Porte TS de `backend/app/core/utils.normalize_phone` (§8.4/§24).
 *
 * DEVE produzir a MESMA forma canônica (E.164 sem '+') que o guard de WhatsApp em
 * Python, pois `handoff_notification_recipients.recipient_normalized` e
 * `internal_whatsapp_blocklist.phone_normalized` são comparados contra o
 * `payload.phone` normalizado no backend. Qualquer divergência abriria buracos na
 * blocklist. Mantenha em sincronia com o util Python.
 */
export function normalizePhone(
  raw: string | null | undefined,
  defaultCountry = '55',
): string | null {
  if (raw === null || raw === undefined) return null;

  let digits = String(raw).replace(/\D/g, '');
  if (!digits) return null;

  digits = digits.replace(/^0+/, '');
  if (!digits) return null;

  const cc = defaultCountry.replace(/\D/g, '') || '55';

  if (cc === '55') {
    if (digits.length === 10 || digits.length === 11) {
      digits = cc + digits;
    } else if (digits.startsWith(cc) && digits.length >= 12) {
      const rest = digits.slice(cc.length);
      if (rest.startsWith(cc) && rest.length >= 12) {
        digits = rest;
      }
    } else if (!digits.startsWith(cc)) {
      digits = cc + digits;
    }
  } else if (!digits.startsWith(cc)) {
    digits = cc + digits;
  }

  return digits;
}
