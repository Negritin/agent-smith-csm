// lib/iron-session.ts
import { SessionOptions } from 'iron-session';

/**
 * Interface para dados de sessão do usuário comum
 */
export interface SessionData {
  userId: string;
  email: string;
  firstName: string;
  lastName: string;
  planId: string | null;
  status: string;
  companyId: string | null;
  companyStatus?: string | null;
  expiresAt: string;
}

/**
 * Interface para dados de sessão do admin
 */
export interface AdminSessionData {
  adminId: string;
  email: string;
  name: string;
  role: 'master_admin' | 'company_admin';
  companyId?: string | null;
  expiresAt: string;
}

/**
 * Configuração do iron-session para usuários
 * O cookie é criptografado automaticamente com AES-256-GCM
 */
export const sessionOptions: SessionOptions = {
  password: process.env.SESSION_SECRET!,
  cookieName: 'smith_user_session',
  cookieOptions: {
    secure: process.env.NODE_ENV === 'production',
    httpOnly: true,
    sameSite: 'lax' as const,
  },
};

/**
 * Configuração do iron-session para admins
 */
export const adminSessionOptions: SessionOptions = {
  password: process.env.SESSION_SECRET!,
  cookieName: 'smith_admin_session',
  cookieOptions: {
    secure: process.env.NODE_ENV === 'production',
    httpOnly: true,
    sameSite: 'lax' as const,
  },
};

/**
 * Helper para criar expiração de sessão
 */
export function createExpiration(rememberMe: boolean = false): string {
  const days = rememberMe ? 30 : 7;
  return new Date(Date.now() + days * 24 * 60 * 60 * 1000).toISOString();
}
