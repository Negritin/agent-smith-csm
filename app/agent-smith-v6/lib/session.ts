import { UserV2 } from './types';

const SESSION_KEY = 'smith_user_session';
const SESSION_EXPIRY_DAYS = 7;

export interface SessionData {
  userId: string;
  email: string;
  firstName: string;
  lastName: string;
  planId: string | null;
  status: string;
  companyId: string | null;
  companyStatus?: string | null;
  webhookUrl?: string | null;
  expiresAt: string;
}

// BAIXO-002: createSession NAO persiste mais PII em localStorage.
// A sessao real e o cookie iron-session httpOnly (via saveUserSession); a UI
// deriva o contexto autenticado de GET /api/auth/me. Esta funcao apenas monta
// o objeto SessionData consumido pelo iron-session no lado servidor.
export function createSession(
  user: UserV2,
  rememberMe: boolean = false,
  companyData?: { status: string; webhook_url: string } | null,
): SessionData {
  const expiryDays = rememberMe ? 30 : SESSION_EXPIRY_DAYS;
  const expiresAt = new Date(Date.now() + expiryDays * 24 * 60 * 60 * 1000).toISOString();

  const sessionData: SessionData = {
    userId: user.id,
    email: user.email,
    firstName: user.first_name,
    lastName: user.last_name,
    planId: user.plan_id,
    status: user.status || 'pending',
    companyId: user.company_id || null,
    companyStatus: companyData?.status || null,
    webhookUrl: companyData?.webhook_url || null,
    expiresAt,
  };

  return sessionData;
}

export function getSession(): SessionData | null {
  if (typeof window === 'undefined') {
    return null;
  }

  const sessionStr = localStorage.getItem(SESSION_KEY);
  if (!sessionStr) {
    return null;
  }

  try {
    const session: SessionData = JSON.parse(sessionStr);

    if (new Date(session.expiresAt) < new Date()) {
      clearSession();
      return null;
    }

    return session;
  } catch {
    clearSession();
    return null;
  }
}

export function clearSession(): void {
  if (typeof window !== 'undefined') {
    localStorage.removeItem(SESSION_KEY);
  }
}

export function isAuthenticated(): boolean {
  return getSession() !== null;
}

export function requireAuth(): SessionData {
  const session = getSession();
  if (!session) {
    if (typeof window !== 'undefined') {
      window.location.href = '/login';
    }
    throw new Error('Not authenticated');
  }
  return session;
}
