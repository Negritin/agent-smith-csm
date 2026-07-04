import crypto from 'node:crypto';
import { NextRequest, NextResponse } from 'next/server';
import { apiError } from '@/lib/api-error';
import type { WidgetBootstrapPayload } from '@/lib/security/widget-bootstrap';
import {
  extractAllowedDomains,
  getPublicWidgetAgent,
  getSupabaseAnonClient,
  getWidgetBootstrapCookieNonceHash,
  getWidgetBootstrapPayload,
  getWidgetHmacSecret,
  isOriginValueAllowed,
  parseAllowedDomain,
  parseOrigin,
} from '@/lib/security/widget-origin';
import type { OriginCandidate, WidgetAgentPublic } from '@/lib/security/widget-origin';

const WIDGET_TOKEN_TTL_SECONDS = 30 * 60;

interface WidgetTokenPayload {
  v: 1;
  kind: 'widget-read';
  agentId: string;
  companyId: string;
  sessionId: string;
  origin: string;
  nonce: string;
  iat: number;
  exp: number;
}

interface WidgetConversationPublic {
  id: string;
  status: string;
  company_id: string;
  agent_id: string | null;
}

interface WidgetMessagesRpcResult {
  agent?: WidgetAgentPublic | null;
  conversation?: WidgetConversationPublic | null;
  messages?: unknown[];
}

interface WidgetRpcProofPayload {
  agentId: string;
  companyId: string;
  sessionId: string;
  origin: string;
  exp: number;
}

function signPayload(encodedPayload: string, secret: string): string {
  return crypto.createHmac('sha256', secret).update(encodedPayload).digest('base64url');
}

function createWidgetToken(payload: WidgetTokenPayload, secret: string): string {
  const encodedPayload = Buffer.from(JSON.stringify(payload), 'utf8').toString('base64url');
  const signature = signPayload(encodedPayload, secret);
  return `${encodedPayload}.${signature}`;
}

function createWidgetRpcProof(payload: WidgetRpcProofPayload, secret: string): string {
  const canonicalPayload = [
    'widget-messages:v1',
    payload.sessionId,
    payload.companyId,
    payload.agentId,
    payload.origin,
    String(payload.exp),
  ].join('\n');

  return crypto.createHmac('sha256', secret).update(canonicalPayload).digest('hex');
}

function verifyWidgetToken(token: string, secret: string): WidgetTokenPayload {
  const [encodedPayload, signature] = token.split('.');
  if (!encodedPayload || !signature) {
    throw new Error('malformed_token');
  }

  const expectedSignature = signPayload(encodedPayload, secret);
  const signatureBuffer = Buffer.from(signature);
  const expectedBuffer = Buffer.from(expectedSignature);

  if (
    signatureBuffer.length !== expectedBuffer.length ||
    !crypto.timingSafeEqual(signatureBuffer, expectedBuffer)
  ) {
    throw new Error('invalid_signature');
  }

  const payload = JSON.parse(Buffer.from(encodedPayload, 'base64url').toString('utf8'));
  if (
    payload?.v !== 1 ||
    payload.kind !== 'widget-read' ||
    typeof payload.agentId !== 'string' ||
    typeof payload.companyId !== 'string' ||
    typeof payload.sessionId !== 'string' ||
    typeof payload.origin !== 'string' ||
    typeof payload.nonce !== 'string' ||
    typeof payload.exp !== 'number'
  ) {
    throw new Error('invalid_payload');
  }

  const nowSeconds = Math.floor(Date.now() / 1000);
  if (payload.exp <= nowSeconds) {
    throw new Error('expired_token');
  }

  return payload as WidgetTokenPayload;
}

function getOriginCandidates(request: NextRequest): OriginCandidate[] {
  const rawCandidates = [request.headers.get('origin'), request.headers.get('referer')];

  const candidates: OriginCandidate[] = [];
  const seen = new Set<string>();

  for (const raw of rawCandidates) {
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

function formatOrigin(candidate: OriginCandidate): string {
  return `${candidate.protocol}//${candidate.host}`;
}

function getAllowedOrigin(request: NextRequest, widgetConfig: unknown): string | null {
  const allowedDomains = extractAllowedDomains(widgetConfig);
  if (allowedDomains.length === 0) {
    return null;
  }

  const candidates = getOriginCandidates(request);
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
          return formatOrigin(candidate);
        }
        continue;
      }

      if (allowed.hasPort && candidate.host === allowed.host) {
        return formatOrigin(candidate);
      }

      if (!allowed.hasPort && candidate.hostname === allowed.hostname) {
        return formatOrigin(candidate);
      }
    }
  }

  return null;
}

function isSameOriginEmbedReferer(request: NextRequest, agentId: string): boolean {
  const referer = request.headers.get('referer');
  if (!referer) return false;

  try {
    const requestUrl = new URL(request.url);
    const refererUrl = new URL(referer);
    const embedPath = `/embed/${agentId}`;

    return (
      refererUrl.origin === requestUrl.origin &&
      (refererUrl.pathname === embedPath || refererUrl.pathname.startsWith(`${embedPath}/`))
    );
  } catch {
    return false;
  }
}

function hasWidgetRequestContext(
  request: NextRequest,
  widgetConfig: unknown,
  agentId: string,
  signedOrigin: string,
): boolean {
  const requestAllowedOrigin = getAllowedOrigin(request, widgetConfig);
  if (requestAllowedOrigin && requestAllowedOrigin === signedOrigin) {
    return true;
  }

  return isSameOriginEmbedReferer(request, agentId);
}

function asWidgetMessagesResult(data: unknown): WidgetMessagesRpcResult | null {
  if (!data || typeof data !== 'object') return null;
  return data as WidgetMessagesRpcResult;
}

/**
 * POST /api/widget/messages
 *
 * Issues a short-lived HMAC token for a widget session after validating the
 * embedding origin against the agent allowlist.
 */
export async function POST(request: NextRequest) {
  try {
    const secret = getWidgetHmacSecret();
    if (!secret) {
      return apiError('Widget security is not configured', { request, status: 403 });
    }

    const body = await request.json().catch(() => ({}));
    const agentId = typeof body.agent_id === 'string' ? body.agent_id : body.agentId;
    const sessionId = typeof body.session_id === 'string' ? body.session_id : body.sessionId;
    const bootstrapToken =
      typeof body.bootstrap_token === 'string' ? body.bootstrap_token : body.bootstrapToken;

    if (typeof agentId !== 'string' || typeof sessionId !== 'string' || sessionId.length > 160) {
      return apiError('agent_id and session_id are required', { request, status: 400 });
    }

    if (typeof bootstrapToken !== 'string') {
      return apiError('Widget bootstrap is required', { request, status: 401 });
    }

    let bootstrapPayload: WidgetBootstrapPayload;
    try {
      bootstrapPayload = getWidgetBootstrapPayload(request, bootstrapToken, secret);
    } catch {
      return apiError('Invalid widget bootstrap', { request, status: 401 });
    }

    const agent = await getPublicWidgetAgent(agentId);
    if (!agent) {
      return apiError('Widget not found', { request, status: 404 });
    }

    if (
      bootstrapPayload.agentId !== agent.id ||
      bootstrapPayload.companyId !== agent.company_id
    ) {
      return apiError('Invalid widget bootstrap', { request, status: 401 });
    }

    if (!isOriginValueAllowed(bootstrapPayload.origin, agent.widget_config)) {
      return apiError('Origin not allowed', { request, status: 403 });
    }

    const nowSeconds = Math.floor(Date.now() / 1000);
    const payload: WidgetTokenPayload = {
      v: 1,
      kind: 'widget-read',
      agentId: agent.id,
      companyId: agent.company_id,
      sessionId,
      origin: bootstrapPayload.origin,
      nonce: bootstrapPayload.nonce,
      iat: nowSeconds,
      exp: nowSeconds + WIDGET_TOKEN_TTL_SECONDS,
    };

    return NextResponse.json({
      token: createWidgetToken(payload, secret),
      expires_at: new Date(payload.exp * 1000).toISOString(),
    });
  } catch (error) {
    console.error('[WIDGET TOKEN API] Error:', error);
    return apiError('Internal server error', {
      cause: error,
      logMessage: '[WIDGET TOKEN API] Error',
      request,
      status: 500,
    });
  }
}

/**
 * GET /api/widget/messages?session_id=xxx
 *
 * Public polling API for widget messages during Human Handoff.
 */
export async function GET(request: NextRequest) {
  try {
    const secret = getWidgetHmacSecret();
    if (!secret) {
      return apiError('Widget security is not configured', { request, status: 403 });
    }

    const { searchParams } = new URL(request.url);
    const sessionId = searchParams.get('session_id');
    const token = request.headers.get('x-widget-token');

    if (!sessionId) {
      return apiError('session_id is required', { request, status: 400 });
    }

    if (!token) {
      return apiError('Widget token is required', { request, status: 401 });
    }

    let tokenPayload: WidgetTokenPayload;
    try {
      tokenPayload = verifyWidgetToken(token, secret);
    } catch {
      return apiError('Invalid widget token', { request, status: 401 });
    }

    if (tokenPayload.sessionId !== sessionId) {
      return apiError('Invalid widget token', { request, status: 401 });
    }

    try {
      if (getWidgetBootstrapCookieNonceHash(request) !== tokenPayload.nonce) {
        return apiError('Invalid widget token', { request, status: 401 });
      }
    } catch {
      return apiError('Invalid widget token', { request, status: 401 });
    }

    const agent = await getPublicWidgetAgent(tokenPayload.agentId);
    if (!agent || agent.company_id !== tokenPayload.companyId) {
      return apiError('Widget not found', { request, status: 404 });
    }

    if (!isOriginValueAllowed(tokenPayload.origin, agent.widget_config)) {
      return apiError('Origin not allowed', { request, status: 403 });
    }

    if (
      !hasWidgetRequestContext(
        request,
        agent.widget_config,
        tokenPayload.agentId,
        tokenPayload.origin,
      )
    ) {
      return apiError('Origin not allowed', { request, status: 403 });
    }

    const rpcProofExpiresAt = Math.floor(Date.now() / 1000) + 60;
    const rpcProof = createWidgetRpcProof(
      {
        agentId: tokenPayload.agentId,
        companyId: tokenPayload.companyId,
        sessionId,
        origin: tokenPayload.origin,
        exp: rpcProofExpiresAt,
      },
      secret,
    );

    const supabaseAnon = getSupabaseAnonClient();
    const { data: messagesResultData, error: messagesError } = await supabaseAnon.rpc(
      'get_widget_messages_scoped',
      {
        p_agent_id: tokenPayload.agentId,
        p_company_id: tokenPayload.companyId,
        p_exp: rpcProofExpiresAt,
        p_origin: tokenPayload.origin,
        p_proof: rpcProof,
        p_session_id: sessionId,
      },
    );

    if (messagesError) {
      console.error('[WIDGET MESSAGES API] Messages RPC error:', messagesError);
      return apiError('Error fetching messages', {
        cause: messagesError,
        logMessage: '[WIDGET MESSAGES API] Messages RPC error',
        request,
        status: 500,
      });
    }

    const messagesResult = asWidgetMessagesResult(messagesResultData);
    const conversation = messagesResult?.conversation;

    if (!conversation) {
      return apiError('Conversation not found', { request, status: 404 });
    }

    if (conversation.agent_id !== tokenPayload.agentId) {
      return apiError('Invalid widget token', { request, status: 401 });
    }

    return NextResponse.json({
      messages: Array.isArray(messagesResult?.messages) ? messagesResult.messages : [],
      status: conversation.status,
      conversationId: conversation.id,
    });
  } catch (error) {
    console.error('[WIDGET MESSAGES API] Error:', error);
    return apiError('Internal server error', {
      cause: error,
      logMessage: '[WIDGET MESSAGES API] Error',
      request,
      status: 500,
    });
  }
}
