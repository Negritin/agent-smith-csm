import crypto from 'node:crypto';

export const WIDGET_BOOTSTRAP_COOKIE_NAME = 'smith_widget_bootstrap';
export const WIDGET_BOOTSTRAP_TTL_SECONDS = 30 * 60;

export interface WidgetBootstrapPayload {
  v: 1;
  kind: 'widget-bootstrap';
  agentId: string;
  companyId: string;
  origin: string;
  nonce: string;
  iat: number;
  exp: number;
}

function signPayload(encodedPayload: string, secret: string): string {
  return crypto.createHmac('sha256', secret).update(encodedPayload).digest('base64url');
}

export function createWidgetBootstrapToken(
  payload: WidgetBootstrapPayload,
  secret: string,
): string {
  const encodedPayload = Buffer.from(JSON.stringify(payload), 'utf8').toString('base64url');
  const signature = signPayload(encodedPayload, secret);
  return `${encodedPayload}.${signature}`;
}

export function verifyWidgetBootstrapToken(token: string, secret: string): WidgetBootstrapPayload {
  const [encodedPayload, signature] = token.split('.');
  if (!encodedPayload || !signature) {
    throw new Error('malformed_bootstrap');
  }

  const expectedSignature = signPayload(encodedPayload, secret);
  const signatureBuffer = Buffer.from(signature);
  const expectedBuffer = Buffer.from(expectedSignature);

  if (
    signatureBuffer.length !== expectedBuffer.length ||
    !crypto.timingSafeEqual(signatureBuffer, expectedBuffer)
  ) {
    throw new Error('invalid_bootstrap_signature');
  }

  const payload = JSON.parse(Buffer.from(encodedPayload, 'base64url').toString('utf8'));
  if (
    payload?.v !== 1 ||
    payload.kind !== 'widget-bootstrap' ||
    typeof payload.agentId !== 'string' ||
    typeof payload.companyId !== 'string' ||
    typeof payload.origin !== 'string' ||
    typeof payload.nonce !== 'string' ||
    typeof payload.exp !== 'number'
  ) {
    throw new Error('invalid_bootstrap_payload');
  }

  const nowSeconds = Math.floor(Date.now() / 1000);
  if (payload.exp <= nowSeconds) {
    throw new Error('expired_bootstrap');
  }

  return payload as WidgetBootstrapPayload;
}
