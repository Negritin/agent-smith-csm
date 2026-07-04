import crypto from 'node:crypto';
import { headers } from 'next/headers';
import { createClient } from '@supabase/supabase-js';
import {
  createWidgetBootstrapToken,
  WIDGET_BOOTSTRAP_TTL_SECONDS,
} from '@/lib/security/widget-bootstrap';
import EmbedChatClient from './EmbedChatClient';

export const dynamic = 'force-dynamic';

const WIDGET_BOOTSTRAP_NONCE_HEADER = 'x-widget-bootstrap-nonce';

interface EmbedPageProps {
  params: Promise<{ agentId: string }>;
}

interface OriginCandidate {
  protocol: string;
  host: string;
  hostname: string;
}

interface WidgetAgentPublic {
  id: string;
  company_id: string;
  widget_config: unknown;
}

function getSupabaseAnonClient() {
  return createClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    { auth: { persistSession: false } },
  );
}

function getWidgetHmacSecret(): string | null {
  return process.env.WIDGET_HMAC_SECRET || null;
}

function parseOrigin(value: string): OriginCandidate | null {
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

function getOriginCandidates(origin: string | null, referer: string | null): OriginCandidate[] {
  const candidates: OriginCandidate[] = [];
  const seen = new Set<string>();

  for (const raw of [origin, referer]) {
    if (!raw) continue;
    const parsed = parseOrigin(raw);
    if (!parsed) continue;

    const key = `${parsed.protocol}//${parsed.host}`;
    if (!seen.has(key)) {
      seen.add(key);
      candidates.push(parsed);
    }
  }

  return candidates;
}

function extractAllowedDomains(widgetConfig: unknown): string[] {
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

function parseAllowedDomain(value: string): OriginCandidate & { wildcard: boolean; hasPort: boolean } | null {
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

function getAllowedOrigin(
  origin: string | null,
  referer: string | null,
  widgetConfig: unknown,
): string | null {
  const allowedDomains = extractAllowedDomains(widgetConfig);
  if (allowedDomains.length === 0) {
    return null;
  }

  const candidates = getOriginCandidates(origin, referer);
  if (candidates.length === 0) {
    return null;
  }

  for (const allowedDomain of allowedDomains) {
    const allowed = parseAllowedDomain(allowedDomain);
    if (!allowed) continue;

    for (const candidate of candidates) {
      if (allowed.protocol && candidate.protocol !== allowed.protocol) {
        continue;
      }

      if (allowed.wildcard) {
        if (
          candidate.hostname === allowed.hostname ||
          candidate.hostname.endsWith(`.${allowed.hostname}`)
        ) {
          return `${candidate.protocol}//${candidate.host}`;
        }
        continue;
      }

      if (allowed.hasPort && candidate.host === allowed.host) {
        return `${candidate.protocol}//${candidate.host}`;
      }

      if (!allowed.hasPort && candidate.hostname === allowed.hostname) {
        return `${candidate.protocol}//${candidate.host}`;
      }
    }
  }

  return null;
}

function asWidgetAgent(data: unknown): WidgetAgentPublic | null {
  if (!data || typeof data !== 'object') return null;
  const value = data as Record<string, unknown>;
  if (typeof value.id !== 'string' || typeof value.company_id !== 'string') return null;

  return {
    id: value.id,
    company_id: value.company_id,
    widget_config: value.widget_config,
  };
}

async function getPublicWidgetAgent(agentId: string): Promise<WidgetAgentPublic | null> {
  const supabaseAnon = getSupabaseAnonClient();
  const { data, error } = await supabaseAnon.rpc('get_widget_agent_public', {
    p_agent_id: agentId,
  });

  if (error) {
    console.error('[WIDGET EMBED] Agent RPC error:', error);
    return null;
  }

  return asWidgetAgent(data);
}

async function createBootstrapToken(agentId: string): Promise<string | null> {
  const secret = getWidgetHmacSecret();
  if (!secret) return null;

  const requestHeaders = await headers();
  const bootstrapNonce = requestHeaders.get(WIDGET_BOOTSTRAP_NONCE_HEADER);
  if (!bootstrapNonce) return null;

  const agent = await getPublicWidgetAgent(agentId);
  if (!agent) return null;

  const allowedOrigin = getAllowedOrigin(
    requestHeaders.get('origin'),
    requestHeaders.get('referer'),
    agent.widget_config,
  );
  if (!allowedOrigin) return null;

  const nowSeconds = Math.floor(Date.now() / 1000);
  return createWidgetBootstrapToken(
    {
      v: 1,
      kind: 'widget-bootstrap',
      agentId: agent.id,
      companyId: agent.company_id,
      origin: allowedOrigin,
      nonce: crypto.createHash('sha256').update(bootstrapNonce).digest('base64url'),
      iat: nowSeconds,
      exp: nowSeconds + WIDGET_BOOTSTRAP_TTL_SECONDS,
    },
    secret,
  );
}

export default async function EmbedPage({ params }: EmbedPageProps) {
  const { agentId } = await params;
  const widgetBootstrapToken = await createBootstrapToken(agentId);

  return <EmbedChatClient widgetBootstrapToken={widgetBootstrapToken} />;
}
