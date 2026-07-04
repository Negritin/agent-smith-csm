import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';
import { getIronSession } from 'iron-session';
import { sessionOptions, adminSessionOptions, SessionData, AdminSessionData } from '@/lib/iron-session';

/**
 * Helper para ler sessão de usuário do cookie criptografado
 * No Edge Runtime, precisamos passar um objeto compatível com iron-session
 */
async function getUserSession(request: NextRequest): Promise<SessionData | null> {
  try {
    // Criar um objeto de resposta para ler o cookie
    const res = new Response();
    const session = await getIronSession<SessionData>(request, res, sessionOptions);

    // Verificar se sessão existe e não expirou
    if (!session.userId) {
      // console.log('[MIDDLEWARE] No userId in session');
      return null;
    }
    if (session.expiresAt && new Date(session.expiresAt) < new Date()) {
      // console.log('[MIDDLEWARE] Session expired');
      return null;
    }

    return session;
  } catch (error) {
    console.error('[MIDDLEWARE] Error reading user session:', error);
    return null;
  }
}

/**
 * Helper para ler sessão de admin do cookie criptografado
 */
async function getAdminSession(request: NextRequest): Promise<AdminSessionData | null> {
  try {
    const res = new Response();
    const session = await getIronSession<AdminSessionData>(request, res, adminSessionOptions);

    // Verificar se sessão existe e não expirou
    if (!session.adminId) {
      // console.log('[MIDDLEWARE] No adminId in session');
      return null;
    }
    if (session.expiresAt && new Date(session.expiresAt) < new Date()) {
      // console.log('[MIDDLEWARE] Admin session expired');
      return null;
    }

    return session;
  } catch (error) {
    console.error('[MIDDLEWARE] Error reading admin session:', error);
    return null;
  }
}

export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const isEmbedRoute = pathname.startsWith('/embed/');

  // console.log(`[MIDDLEWARE] ${request.method} ${pathname}`);

  // =========================================================================
  // 🛡️ SECURITY HEADERS (CSP & HARDENING)
  // =========================================================================

  // Criamos a resposta base para injetar headers
  const widgetBootstrapNonce = isEmbedRoute ? crypto.randomUUID() : null;
  const requestHeaders = new Headers(request.headers);
  if (widgetBootstrapNonce) {
    requestHeaders.set('x-widget-bootstrap-nonce', widgetBootstrapNonce);
  }

  const response = widgetBootstrapNonce
    ? NextResponse.next({ request: { headers: requestHeaders } })
    : NextResponse.next();

  if (widgetBootstrapNonce) {
    const forwardedProto = request.headers.get('x-forwarded-proto')?.split(',')[0]?.trim();
    const isSecureRequest = request.nextUrl.protocol === 'https:' || forwardedProto === 'https';
    response.cookies.set('smith_widget_bootstrap', widgetBootstrapNonce, {
      httpOnly: true,
      maxAge: 30 * 60,
      path: '/',
      sameSite: isSecureRequest ? 'none' : 'lax',
      secure: isSecureRequest,
      // CHIPS (Partitioned): permite o nonce-cookie funcionar no iframe do widget
      // embarcado em OUTRO domínio. Sem isso, navegadores modernos (Chrome pós
      // phase-out de 3rd-party cookies, Safari ITP, Firefox) bloqueiam o cookie de
      // terceiro -> o /api/leads/identify não acha o nonce -> 401. Só vale com
      // SameSite=None; Secure, por isso condicionado a isSecureRequest.
      partitioned: isSecureRequest,
    });
  }

  // Headers Globais de Segurança
  response.headers.set('X-Content-Type-Options', 'nosniff');
  response.headers.set('Referrer-Policy', 'strict-origin-when-cross-origin');
  response.headers.set('Strict-Transport-Security', 'max-age=63072000; includeSubDomains; preload');

  // Lógica Condicional: WIDGET (Embed) vs RESTO (Admin/Dashboard)
  if (isEmbedRoute) {
    // 🟢 WIDGET: Permitir ser carregado em iframes de QUALQUER origem (*)
    // Isso é essencial para o widget funcionar no site dos clientes.
    response.headers.set('Content-Security-Policy', "frame-ancestors *;");
    response.headers.delete('X-Frame-Options'); // Importante remover se existir
  } else {
    // 🔴 RESTO: Bloquear iframes (Proteção contra Clickjacking)
    // Impede que o painel admin ou dashboard sejam carregados dentro de um site malicioso.
    response.headers.set('X-Frame-Options', 'DENY');
    response.headers.set('Content-Security-Policy', "frame-ancestors 'none';");
  }

  // =========================================================================
  // ROTAS E AUTENTICAÇÃO
  // =========================================================================

  const publicRoutes = [
    '/',
    '/landing',
    '/login',
    '/register',
    '/pending-approval',
    '/no-company',
    '/company-suspended',
    '/admin/login',
    '/forgot-password',
    '/reset-password',
    '/billing/success',
    '/billing/cancel',
  ];

  // Rotas que começam com estes prefixos são públicas
  const publicPrefixes = [
    '/embed/',  // Widget embed pages (public)
  ];

  const apiRoutes = [
    '/api/auth/login',
    '/api/auth/signup',
    '/api/auth/logout',
    '/api/auth/me',
    '/api/admin/login',
    '/api/admin/logout',
  ];

  const isPublicRoute = publicRoutes.includes(pathname);
  const isPublicPrefix = publicPrefixes.some(prefix => pathname.startsWith(prefix));
  const isApiRoute = apiRoutes.includes(pathname) || pathname.startsWith('/api/');
  const isAdminRoute = pathname.startsWith('/admin') && pathname !== '/admin/login';

  // === TRATAMENTO DE ROTAS PÚBLICAS E API ===
  if (isPublicRoute || isPublicPrefix || isApiRoute) {
    // Retorna a resposta que JA TEM os headers de segurança configurados acima
    return response;
  }

  // === ADMIN ROUTES ===
  if (isAdminRoute) {
    const adminSession = await getAdminSession(request);

    // console.log('[MIDDLEWARE] Admin Route Check: ', { hasSession: !!adminSession });

    if (!adminSession) {
      // console.log('[MIDDLEWARE] Invalid or expired admin session, redirecting');
      const loginUrl = new URL('/admin/login', request.url);
      const redirectResp = NextResponse.redirect(loginUrl);
      // Restaurando limpeza de cookie
      redirectResp.cookies.delete('smith_admin_session');
      // Aplicar proteção de clickjacking no redirect também
      redirectResp.headers.set('X-Frame-Options', 'DENY');
      return redirectResp;
    }

    // console.log('[MIDDLEWARE] Admin session valid, allowing access');
    return response;
  }

  // === USER ROUTES ===
  const session = await getUserSession(request);

  // DEBUG LOG - REMOVER DEPOIS
  // DEBUG LOG - REMOVED FOR SECURITY
  // console.log('[MIDDLEWARE] User Session Debug:', { hasSession: !!session });

  if (!session) {
    const loginUrl = new URL('/login', request.url);
    loginUrl.searchParams.set('redirect', pathname);
    const redirectResp = NextResponse.redirect(loginUrl);
    // Restaurando limpeza de cookie
    redirectResp.cookies.delete('smith_user_session');
    redirectResp.headers.set('X-Frame-Options', 'DENY');
    return redirectResp;
  }

  // Dashboard-specific validations
  if (pathname.startsWith('/dashboard')) {
    const userStatus = session.status || 'pending';
    const companyId = session.companyId;
    const companyStatus = session.companyStatus;

    if (userStatus === 'pending') {
      return NextResponse.redirect(new URL('/pending-approval', request.url));
    }

    if (userStatus === 'suspended') {
      return NextResponse.redirect(new URL('/company-suspended', request.url));
    }

    if (!companyId) {
      return NextResponse.redirect(new URL('/no-company', request.url));
    }

    if (companyStatus === 'suspended' || companyStatus === 'cancelled') {
      return NextResponse.redirect(new URL('/company-suspended', request.url));
    }
  }

  // Se tudo passou, retorna a resposta com headers
  return response;
}

export const config = {
  matcher: [
    '/((?!_next/static|_next/image|favicon.ico|.*\\..*|api/auth).*)',
  ],
};
