import { NextRequest, NextResponse } from 'next/server';
import { loginAdmin } from '@/lib/auth';
import { apiError } from '@/lib/api-error';
import { log, logSystemAction, getClientInfo, errorLogFields } from '@/lib/logger';
import { AdminSessionData } from '@/lib/iron-session';
import { saveAdminSession } from '@/lib/auth-actions';
import { logSecurityAudit } from '@/lib/security-audit';
import { rateLimit, RATE_LIMITS, getRateLimitHeaders } from '@/lib/rate-limit';

export const dynamic = 'force-dynamic';

export async function POST(request: NextRequest) {
  const { ipAddress, userAgent } = getClientInfo(request);
  const ip = ipAddress || 'unknown';

  try {
    const { email, password } = await request.json();

    log.debug('[ADMIN LOGIN API] Request received', { loginIdentifierPresent: Boolean(email) });

    if (!email || !password) {
      return apiError('Email e senha são obrigatórios', { request, status: 400 });
    }

    const normalizedEmail = String(email).toLowerCase().trim();

    // =============================================
    // RATE LIMITING (ALTO-001): por IP e por e-mail (5/15min cada)
    // Aplicado ANTES de loginAdmin para frear brute-force/credential stuffing.
    // Erro generico ('Muitas tentativas...') — nao revela existencia de conta.
    // =============================================
    const ipLimit = await rateLimit(
      `admin-login:ip:${ip}`,
      RATE_LIMITS.ADMIN_LOGIN_IP.maxRequests,
      RATE_LIMITS.ADMIN_LOGIN_IP.windowMs,
    );

    if (!ipLimit.success) {
      log.warn('[ADMIN LOGIN API] IP rate limit exceeded', { ip });
      return apiError('Muitas tentativas. Aguarde antes de tentar novamente.', {
        headers: getRateLimitHeaders(ipLimit),
        request,
        status: 429,
      });
    }

    const emailLimit = await rateLimit(
      `admin-login:email:${normalizedEmail}`,
      RATE_LIMITS.ADMIN_LOGIN_EMAIL.maxRequests,
      RATE_LIMITS.ADMIN_LOGIN_EMAIL.windowMs,
    );

    if (!emailLimit.success) {
      log.warn('[ADMIN LOGIN API] Email rate limit exceeded');
      return apiError('Muitas tentativas. Aguarde antes de tentar novamente.', {
        headers: getRateLimitHeaders(emailLimit),
        request,
        status: 429,
      });
    }

    const { admin, error } = await loginAdmin(email, password);

    log.debug('[ADMIN LOGIN API] Login result', {
      adminFound: Boolean(admin),
      hasError: Boolean(error),
    });

    if (error || !admin) {
      await logSecurityAudit({
        action: 'admin_login_failed',
        request,
        status: 'error',
        errorMessage: error || 'Falha no login do admin',
        details: {
          loginIdentifierPresent: Boolean(email),
        },
      });

      await logSystemAction({
        actionType: 'ADMIN_LOGIN',
        details: { email, error },
        ipAddress,
        userAgent,
        status: 'error',
        errorMessage: error || 'Falha no login do admin',
      });

      return apiError(error || 'Erro ao fazer login', { request, status: 401 });
    }

    if (admin.role !== 'master_admin' && admin.role !== 'company_admin') {
      await logSecurityAudit({
        action: 'admin_login_failed',
        actorId: admin.id,
        actorRole: admin.role,
        request,
        status: 'error',
        errorMessage: 'Invalid admin role',
        details: {
          failureReason: 'invalid_role',
        },
      });
      return apiError('Erro ao fazer login', { request, status: 401 });
    }

    if (admin.role === 'company_admin' && !admin.companyId) {
      await logSecurityAudit({
        action: 'admin_login_failed',
        actorId: admin.id,
        actorRole: admin.role,
        request,
        status: 'error',
        errorMessage: 'Company admin missing companyId',
        details: {
          failureReason: 'missing_company_id',
        },
      });
      return apiError('Erro ao fazer login', { request, status: 401 });
    }

    await logSecurityAudit({
      action: 'admin_login_success',
      actorId: admin.id,
      actorRole: admin.role,
      companyId: admin.companyId || null,
      resourceType: admin.role === 'master_admin' ? 'admin_users' : 'users_v2',
      resourceId: admin.id,
      request,
      status: 'success',
      details: {
        sessionType: admin.role,
      },
    });

    await logSystemAction({
      adminId: admin.id,
      actionType: 'ADMIN_LOGIN',
      resourceType: 'admin',
      resourceId: admin.id,
      details: {
        email: admin.email,
        name: admin.name,
      },
      ipAddress,
      userAgent,
      sessionId: admin.id,
      status: 'success',
    });

    const maxAge = 8 * 60 * 60; // 8 horas
    const session: AdminSessionData = {
      adminId: admin.id,
      email: admin.email,
      name: admin.name,
      role: admin.role,
      companyId: admin.companyId || null,
      expiresAt: new Date(Date.now() + maxAge * 1000).toISOString(),
    };

    await saveAdminSession(session);

    return NextResponse.json(
      {
        success: true,
        admin: {
          id: admin.id,
          email: admin.email,
          name: admin.name,
          role: session.role,
          companyId: admin.companyId,
        },
      },
      { status: 200 },
    );
  } catch (error: unknown) {
    log.error('[ADMIN LOGIN API] Error', errorLogFields(error));

    await logSystemAction({
      actionType: 'ERROR_OCCURRED',
      details: { ...errorLogFields(error), endpoint: '/api/admin/login' },
      ipAddress,
      userAgent,
      status: 'error',
      errorMessage: 'Erro ao processar login',
    });

    return apiError('Erro ao processar login', {
      cause: error,
      logMessage: '[ADMIN LOGIN API] Error',
      request,
      status: 500,
    });
  }
}
