import { createHmac } from 'crypto';
import { cookies } from 'next/headers';
import { getIronSession } from 'iron-session';
import {
  adminSessionOptions,
  sessionOptions,
  type AdminSessionData,
  type SessionData,
} from './iron-session';
import { log } from './logger';

const INTERNAL_JWT_TTL_SECONDS = 5 * 60;

type InternalJwtActorType = 'user' | 'company_admin' | 'master_admin';

export interface InternalJwtClaims {
  user_id?: string;
  company_id: string;
  role: string;
  actor_type: InternalJwtActorType;
  admin_id?: string;
  iat: number;
  exp: number;
}

export interface InternalJwtInput {
  userId?: string;
  companyId: string;
  role: string;
  actorType?: InternalJwtActorType;
  adminId?: string;
  ttlSeconds?: number;
}

type HeaderMap = Record<string, string>;

function getInternalJwtSecret(): string {
  const secret = process.env.INTERNAL_JWT_SECRET;

  if (!secret || secret.length < 32) {
    log.error('[INTERNAL JWT] Secret is not configured or is too short');
    throw new Error('INTERNAL_JWT_SECRET_NOT_CONFIGURED');
  }

  return secret;
}

function base64Url(value: string | Buffer): string {
  const buffer = typeof value === 'string' ? Buffer.from(value, 'utf8') : value;
  return buffer.toString('base64url');
}

export function signInternalJwt(input: InternalJwtInput): string {
  const now = Math.floor(Date.now() / 1000);
  const ttl = input.ttlSeconds ?? INTERNAL_JWT_TTL_SECONDS;
  const actorType = input.actorType ?? 'user';

  if (actorType === 'user' && !input.userId) {
    throw new Error('INTERNAL_JWT_USER_ID_MISSING');
  }
  if ((actorType === 'company_admin' || actorType === 'master_admin') && !input.adminId) {
    throw new Error('INTERNAL_JWT_ADMIN_ID_MISSING');
  }

  const claims: InternalJwtClaims = {
    company_id: input.companyId,
    role: input.role,
    actor_type: actorType,
    iat: now,
    exp: now + ttl,
  };

  if (input.userId) {
    claims.user_id = input.userId;
  }
  if (input.adminId) {
    claims.admin_id = input.adminId;
  }

  const encodedHeader = base64Url(JSON.stringify({ alg: 'HS256', typ: 'JWT' }));
  const encodedPayload = base64Url(JSON.stringify(claims));
  const signingInput = `${encodedHeader}.${encodedPayload}`;
  const signature = createHmac('sha256', getInternalJwtSecret())
    .update(signingInput)
    .digest();

  return `${signingInput}.${base64Url(signature)}`;
}

export function createInternalAuthHeaders(input: InternalJwtInput): HeaderMap {
  const token = signInternalJwt(input);
  return {
    Authorization: `Bearer ${token}`,
    'X-Internal-JWT': token,
  };
}

function isExpired(expiresAt?: string): boolean {
  return !expiresAt || new Date(expiresAt).getTime() <= Date.now();
}

function getUserRole(session: SessionData): string {
  const role = (session as SessionData & { role?: string }).role;
  return typeof role === 'string' && role.length > 0 ? role : 'member';
}

export function createInternalAuthHeadersForUserSession(
  session: SessionData,
  companyId = session.companyId,
): HeaderMap {
  if (!session.userId || !companyId) {
    throw new Error('INTERNAL_JWT_USER_CONTEXT_MISSING');
  }

  return createInternalAuthHeaders({
    userId: session.userId,
    companyId,
    role: getUserRole(session),
    actorType: 'user',
  });
}

export function createInternalAuthHeadersForAdminSession(
  session: AdminSessionData,
  companyId = session.companyId ?? null,
): HeaderMap {
  if (!session.adminId) {
    throw new Error('INTERNAL_JWT_ADMIN_CONTEXT_MISSING');
  }
  if (!companyId) {
    throw new Error('INTERNAL_JWT_ADMIN_COMPANY_CONTEXT_MISSING');
  }

  return createInternalAuthHeaders({
    companyId,
    role: session.role,
    actorType: session.role,
    adminId: session.adminId,
  });
}

export async function getOptionalInternalAuthHeaders(options: {
  companyId?: string | null;
} = {}): Promise<HeaderMap> {
  try {
    const cookieStore = await cookies();

    const userSession = await getIronSession<SessionData>(cookieStore, sessionOptions);
    if (userSession.userId && !isExpired(userSession.expiresAt)) {
      const companyId = userSession.companyId || options.companyId || null;
      if (!companyId) return {};
      return createInternalAuthHeadersForUserSession(userSession, companyId);
    }

    const adminSession = await getIronSession<AdminSessionData>(
      cookieStore,
      adminSessionOptions,
    );
    if (adminSession.adminId && !isExpired(adminSession.expiresAt)) {
      const companyId = adminSession.companyId || options.companyId || null;
      if (!companyId) return {};
      return createInternalAuthHeadersForAdminSession(adminSession, companyId);
    }
  } catch (cause) {
    log.warn('[INTERNAL JWT] Could not create optional auth header', {
      errorName: cause instanceof Error ? cause.name : undefined,
      errorType: cause instanceof Error ? undefined : typeof cause,
    });
  }

  return {};
}
