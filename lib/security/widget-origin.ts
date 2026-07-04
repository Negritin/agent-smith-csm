import crypto from 'node:crypto';
import type { NextRequest } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import {
  verifyWidgetBootstrapToken,
  WIDGET_BOOTSTRAP_COOKIE_NAME,
} from '@/lib/security/widget-bootstrap';
import type { WidgetBootstrapPayload } from '@/lib/security/widget-bootstrap';

/**
 * Módulo compartilhado de segurança de origem/HMAC do widget (ALTO-005).
 *
 * Centraliza a lógica de proof HMAC de bootstrap, nonce-cookie e matching de
 * allowlist de origem que antes estava DUPLICADA entre os dois endpoints
 * públicos não autenticados: `app/api/widget/messages/route.ts` e
 * `app/api/leads/identify/route.ts`. A fonte é única; o comportamento de
 * validação de origem/HMAC permanece idêntico ao anterior.
 */

export interface OriginCandidate {
  protocol: string;
  host: string;
  hostname: string;
}

export interface WidgetAgentPublic {
  id: string;
  company_id: string;
  widget_config: unknown;
}

export function getSupabaseAnonClient() {
  return createClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    { auth: { persistSession: false } },
  );
}

export function getWidgetHmacSecret(): string | null {
  return process.env.WIDGET_HMAC_SECRET || null;
}

export function hashWidgetBootstrapNonce(nonce: string): string {
  return crypto.createHash('sha256').update(nonce).digest('base64url');
}

export function getWidgetBootstrapCookieNonceHash(request: NextRequest): string {
  const bootstrapCookie = request.cookies.get(WIDGET_BOOTSTRAP_COOKIE_NAME)?.value;
  if (!bootstrapCookie) {
    throw new Error('missing_bootstrap_cookie');
  }

  return hashWidgetBootstrapNonce(bootstrapCookie);
}

export function getWidgetBootstrapPayload(
  request: NextRequest,
  bootstrapToken: string,
  secret: string,
): WidgetBootstrapPayload {
  const payload = verifyWidgetBootstrapToken(bootstrapToken, secret);
  if (payload.nonce !== getWidgetBootstrapCookieNonceHash(request)) {
    throw new Error('bootstrap_cookie_mismatch');
  }

  return payload;
}

export function parseOrigin(value: string): OriginCandidate | null {
  const trimmed = value.trim();
  if (!trimmed) return null;

  try {
    const parsed = new URL(trimmed);
    return {
      protocol: parsed.protocol.toLowerCase(),
      host: parsed.host.toLowerCase(),
      hostname: parsed.hostname.replace(/^\[/, '').replace(/\]$/, '').toLowerCase(),
    };
  } catch {
    return null;
  }
}

export function extractAllowedDomains(widgetConfig: unknown): string[] {
  if (!widgetConfig || typeof widgetConfig !== 'object') {
    return [];
  }

  const config = widgetConfig as Record<string, unknown>;
  const keys = [
    'allowedDomains',
    'allowed_domains',
    'allowedOrigins',
    'allowed_origins',
    'domainAllowlist',
    'originAllowlist',
  ];

  for (const key of keys) {
    const value = config[key];
    if (Array.isArray(value)) {
      return value.filter((item): item is string => typeof item === 'string');
    }
    if (typeof value === 'string') {
      return value
        .split(/[\n,]/)
        .map((item) => item.trim())
        .filter(Boolean);
    }
  }

  return [];
}

export function parseAllowedDomain(
  value: string,
): (OriginCandidate & { wildcard: boolean; hasPort: boolean }) | null {
  const trimmed = value.trim().toLowerCase();
  if (!trimmed || trimmed === '*') return null;

  let protocol = '';
  let hostPattern = trimmed;

  const schemeMatch = trimmed.match(/^([a-z][a-z0-9+.-]*):\/\/(.+)$/i);
  if (schemeMatch) {
    protocol = `${schemeMatch[1].toLowerCase()}:`;
    hostPattern = schemeMatch[2].split('/')[0].toLowerCase();
  } else {
    hostPattern = trimmed.replace(/^\/\//, '').split('/')[0].toLowerCase();
  }

  const wildcard = hostPattern.startsWith('*.');
  const host = wildcard ? hostPattern.slice(2) : hostPattern;
  const hostname = host.split(':')[0];

  if (!hostname) return null;

  return {
    protocol,
    host,
    hostname,
    wildcard,
    hasPort: host.includes(':'),
  };
}

export function isOriginValueAllowed(origin: string, widgetConfig: unknown): boolean {
  const candidate = parseOrigin(origin);
  if (!candidate) return false;

  const allowedDomains = extractAllowedDomains(widgetConfig);
  if (allowedDomains.length === 0) {
    return false;
  }

  for (const allowedDomain of allowedDomains) {
    const allowed = parseAllowedDomain(allowedDomain);
    if (!allowed) continue;

    if (allowed.protocol && candidate.protocol !== allowed.protocol) {
      continue;
    }

    if (allowed.wildcard) {
      if (
        candidate.hostname === allowed.hostname ||
        candidate.hostname.endsWith(`.${allowed.hostname}`)
      ) {
        return true;
      }
      continue;
    }

    if (allowed.hasPort && candidate.host === allowed.host) {
      return true;
    }

    if (!allowed.hasPort && candidate.hostname === allowed.hostname) {
      return true;
    }
  }

  return false;
}

export function asWidgetAgent(data: unknown): WidgetAgentPublic | null {
  if (!data || typeof data !== 'object') return null;
  const value = data as Record<string, unknown>;
  if (typeof value.id !== 'string' || typeof value.company_id !== 'string') return null;

  return {
    id: value.id,
    company_id: value.company_id,
    widget_config: value.widget_config,
  };
}

export async function getPublicWidgetAgent(
  agentId: string,
  logLabel = '[WIDGET API]',
): Promise<WidgetAgentPublic | null> {
  const supabaseAnon = getSupabaseAnonClient();
  const { data, error } = await supabaseAnon.rpc('get_widget_agent_public', {
    p_agent_id: agentId,
  });

  if (error) {
    console.error(`${logLabel} Agent RPC error:`, error);
    return null;
  }

  return asWidgetAgent(data);
}
