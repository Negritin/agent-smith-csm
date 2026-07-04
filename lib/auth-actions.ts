import { cookies } from 'next/headers';
import { NextResponse } from 'next/server';
import { getIronSession, type IronSession } from 'iron-session';
import {
  adminSessionOptions,
  type AdminSessionData,
  sessionOptions,
  type SessionData,
} from './iron-session';
import { hashPassword, validatePasswordStrength, verifyPassword } from './auth';
import { apiError, authApiError } from './api-error';
import { logSecurityAudit } from './security-audit';
import { getSupabaseAdmin } from './supabase-admin';

type CookieStore = Awaited<ReturnType<typeof cookies>>;

type SessionResult<T extends object> =
  | { session: IronSession<T>; response?: never }
  | { session?: never; response: NextResponse };

/**
 * Tipo da sessão admin retornada por `requireAdminSession()` quando autenticada
 * com sucesso (BAIXO-005). Exportado para que as rotas tipem `session` em vez de
 * `any`, preservando a checagem de autorização (`session.role`, `session.companyId`,
 * `session.adminId`).
 */
export type AdminSession = IronSession<AdminSessionData>;

type OptionalSessionResult<T extends object> =
  | { session: IronSession<T>; response?: never }
  | { session?: never; response: NextResponse }
  | { session?: never; response?: never };

const USER_REAUTH_PATH = '/login?reauth=1';
const ADMIN_REAUTH_PATH = '/admin/login?reauth=1';

function isExpired(expiresAt?: string): boolean {
  return !expiresAt || new Date(expiresAt).getTime() <= Date.now();
}

function isValidAdminSession(session: IronSession<AdminSessionData>): boolean {
  if (!session.adminId) return false;
  if (session.role !== 'master_admin' && session.role !== 'company_admin') return false;
  if (session.role === 'company_admin' && !session.companyId) return false;
  return true;
}

function reauthResponse(cookieName: string, _reauthPath: string): NextResponse {
  const response = apiError('Sessão expirada. Faça login novamente.', { status: 401 });
  response.cookies.delete(cookieName);
  return response;
}

async function readSession<T extends object>(
  cookieStore: CookieStore,
  cookieName: string,
  reauthPath: string,
  options: typeof sessionOptions,
  hasIdentity: (session: IronSession<T>) => boolean,
): Promise<SessionResult<T>> {
  const hadCookie = !!cookieStore.get(cookieName)?.value;

  try {
    const session = await getIronSession<T>(cookieStore, options);

    if (!hasIdentity(session)) {
      if (hadCookie) {
        return { response: reauthResponse(cookieName, reauthPath) };
      }

      return {
        response: apiError('Não autorizado', { status: 401 }),
      };
    }

    if (isExpired((session as T & { expiresAt?: string }).expiresAt)) {
      session.destroy();
      return { response: reauthResponse(cookieName, reauthPath) };
    }

    return { session };
  } catch (error) {
    console.warn('[AUTH] Invalid legacy session cookie cleared:', cookieName, error);
    return { response: reauthResponse(cookieName, reauthPath) };
  }
}

async function readOptionalSession<T extends object>(
  cookieStore: CookieStore,
  cookieName: string,
  reauthPath: string,
  options: typeof sessionOptions,
  hasIdentity: (session: IronSession<T>) => boolean,
): Promise<OptionalSessionResult<T>> {
  const hadCookie = !!cookieStore.get(cookieName)?.value;

  try {
    const session = await getIronSession<T>(cookieStore, options);

    if (!hasIdentity(session)) {
      if (hadCookie) {
        return { response: reauthResponse(cookieName, reauthPath) };
      }

      return {};
    }

    if (isExpired((session as T & { expiresAt?: string }).expiresAt)) {
      session.destroy();
      return { response: reauthResponse(cookieName, reauthPath) };
    }

    return { session };
  } catch (error) {
    console.warn('[AUTH] Invalid legacy session cookie cleared:', cookieName, error);
    return { response: reauthResponse(cookieName, reauthPath) };
  }
}

export async function requireAdminSession(): Promise<SessionResult<AdminSessionData>> {
  const cookieStore = await cookies();
  return readSession<AdminSessionData>(
    cookieStore,
    'smith_admin_session',
    ADMIN_REAUTH_PATH,
    adminSessionOptions,
    isValidAdminSession,
  );
}

export async function requireMasterAdminSession(): Promise<SessionResult<AdminSessionData>> {
  const result = await requireAdminSession();

  if (result.response) {
    return result;
  }

  if (result.session.role !== 'master_admin') {
    return {
      response: apiError('Não autorizado', { status: 403 }),
    };
  }

  return result;
}

/**
 * Owner-gate para Métricas (SPEC §2.4 / D2 — só-owner).
 *
 * `AdminSessionData` e o `InternalJwtClaims` do backend NÃO carregam owner, então
 * a checagem precisa bater no banco (`users_v2.is_owner`). Resolve TAMBÉM de qual
 * empresa carregar (validação B4):
 *  - company_admin → companyId = session.companyId; exige `users_v2.is_owner = true`
 *    (escopado por company_id, defesa em profundidade) senão 403.
 *  - master_admin  → bypassa o check de owner (seu adminId está em `admin_users`,
 *    não em `users_v2` → o lookup falharia fail-closed e barraria o próprio master,
 *    SPEC C5); exige `?company_id=` (ou `?companyId=`) na query, senão 400.
 *
 * Mirror exato da resolução de empresa de `requireAttendanceAdmin`
 * (lib/attendance-actions.ts:105-147) + leitura de is_owner de team/approve:30-34.
 * NÃO confiar no spread-guard do menu (client-side) para proteção.
 */
export async function requireOwnerOrMaster(
  request: Request,
): Promise<
  | { companyId: string; session: AdminSession; response?: never }
  | { companyId?: never; session?: never; response: NextResponse }
> {
  const result = await requireAdminSession();
  if (result.response) {
    return { response: await authApiError(result.response, { request }) };
  }
  const session = result.session;

  // master_admin: bypassa owner check, exige company_id na query (mirror attendance).
  if (session.role === 'master_admin') {
    const url = new URL(request.url);
    const queryCompanyId = url.searchParams.get('company_id') || url.searchParams.get('companyId');
    if (!queryCompanyId) {
      return {
        response: apiError('company_id é obrigatório para master_admin', {
          request,
          status: 400,
        }),
      };
    }
    return { companyId: queryCompanyId, session };
  }

  // company_admin: companyId da sessão, depois check de owner no banco.
  if (!session.companyId) {
    return { response: apiError('Não autorizado', { request, status: 403 }) };
  }
  const { data, error } = await getSupabaseAdmin()
    .from('users_v2')
    .select('is_owner')
    .eq('id', session.adminId)
    .eq('company_id', session.companyId)
    .single();
  if (error || !data?.is_owner) {
    return {
      response: apiError('Apenas o Owner pode acessar Métricas', {
        request,
        status: 403,
      }),
    };
  }
  return { companyId: session.companyId, session };
}

export async function requireUserSession(): Promise<SessionResult<SessionData>> {
  const cookieStore = await cookies();
  return readSession<SessionData>(
    cookieStore,
    'smith_user_session',
    USER_REAUTH_PATH,
    sessionOptions,
    (session) => !!session.userId,
  );
}

export async function getUserOrAdminSession(preferAdmin = false): Promise<
  | { userSession: IronSession<SessionData>; adminSession?: never; response?: never }
  | { adminSession: IronSession<AdminSessionData>; userSession?: never; response?: never }
  | { response: NextResponse; userSession?: never; adminSession?: never }
> {
  const cookieStore = await cookies();

  const readUser = () =>
    readOptionalSession<SessionData>(
      cookieStore,
      'smith_user_session',
      USER_REAUTH_PATH,
      sessionOptions,
      (session) => !!session.userId,
    );
  const readAdmin = () =>
    readOptionalSession<AdminSessionData>(
      cookieStore,
      'smith_admin_session',
      ADMIN_REAUTH_PATH,
      adminSessionOptions,
      isValidAdminSession,
    );

  // preferAdmin (rotas de ADMIN, ex.: /api/messages?scope=admin): a sessão de
  // admin tem prioridade. Sem isto, um cookie smith_user_session residual
  // escopava a rota por user_id e dava 404 em conversa de outro user (ex.: o
  // contato do WhatsApp). Default (false) preserva o user-first do dashboard do
  // usuário final — que NÃO tem cookie de admin, então segue inalterado.
  if (preferAdmin) {
    const adminResult = await readAdmin();
    if (adminResult.session) return { adminSession: adminResult.session };
    if (adminResult.response) return { response: adminResult.response };

    const userResult = await readUser();
    if (userResult.session) return { userSession: userResult.session };
    if (userResult.response) return { response: userResult.response };
  } else {
    const userResult = await readUser();
    if (userResult.session) return { userSession: userResult.session };
    if (userResult.response) return { response: userResult.response };

    const adminResult = await readAdmin();
    if (adminResult.session) return { adminSession: adminResult.session };
    if (adminResult.response) return { response: adminResult.response };
  }

  return { response: apiError('Não autorizado', { status: 401 }) };
}

export async function saveUserSession(sessionData: SessionData, rememberMe = false): Promise<void> {
  const maxAge = rememberMe ? 30 * 24 * 60 * 60 : 7 * 24 * 60 * 60;
  const cookieStore = await cookies();
  const session = await getIronSession<SessionData>(cookieStore, {
    ...sessionOptions,
    cookieOptions: { ...sessionOptions.cookieOptions, maxAge },
  });

  Object.assign(session, sessionData);
  await session.save();
}

export async function saveAdminSession(sessionData: AdminSessionData): Promise<void> {
  const maxAge = 8 * 60 * 60;
  const cookieStore = await cookies();
  const session = await getIronSession<AdminSessionData>(cookieStore, {
    ...adminSessionOptions,
    cookieOptions: { ...adminSessionOptions.cookieOptions, maxAge },
  });

  Object.assign(session, sessionData);
  await session.save();
}

export async function destroyUserSession(): Promise<void> {
  const cookieStore = await cookies();
  const session = await getIronSession<SessionData>(cookieStore, sessionOptions);
  session.destroy();
}

export async function destroyAdminSession(): Promise<void> {
  const cookieStore = await cookies();
  const session = await getIronSession<AdminSessionData>(cookieStore, adminSessionOptions);
  session.destroy();
}

export async function changePassword(options: {
  table: 'users_v2' | 'admin_users';
  userId: string;
  currentPassword: string;
  newPassword: string;
  notFoundMessage: string;
  request?: Request;
}): Promise<NextResponse> {
  const validation = validatePasswordStrength(options.newPassword);
  if (!validation.valid) {
    return apiError('A nova senha não atende à política de segurança', {
      request: options.request,
      status: 400,
    });
  }

  const supabaseAdmin = getSupabaseAdmin();
  const { data: user, error: userError } = await supabaseAdmin
    .from(options.table)
    .select('id, password_hash')
    .eq('id', options.userId)
    .single();

  if (userError || !user?.password_hash) {
    return apiError(options.notFoundMessage, { request: options.request, status: 404 });
  }

  const isValid = await verifyPassword(options.currentPassword, user.password_hash);
  if (!isValid) {
    return apiError('Senha atual incorreta', { request: options.request, status: 401 });
  }

  const newHash = await hashPassword(options.newPassword);
  const { error: updateError } = await supabaseAdmin
    .from(options.table)
    .update({ password_hash: newHash, updated_at: new Date().toISOString() })
    .eq('id', options.userId);

  if (updateError) {
    console.error('[AUTH] Password update failed:', updateError);
    return apiError('Erro ao atualizar senha', {
      cause: updateError,
      logMessage: '[AUTH] Password update failed',
      request: options.request,
      status: 500,
    });
  }

  return NextResponse.json({ success: true });
}

export async function auditCrossTenantAttempt(params: {
  actorId: string;
  actorRole: 'master_admin' | 'company_admin';
  actorCompanyId?: string | null;
  resourceType: string;
  resourceId: string;
  targetCompanyId?: string | null;
  action: string;
  request?: Request;
}): Promise<void> {
  await logSecurityAudit({
    action: 'cross_tenant_attempt',
    actorId: params.actorId,
    actorRole: params.actorRole,
    companyId: params.actorCompanyId || null,
    resourceType: params.resourceType,
    resourceId: params.resourceId,
    targetCompanyId: params.targetCompanyId || null,
    request: params.request,
    status: 'error',
    details: {
      attemptedAction: params.action,
    },
  });
}
