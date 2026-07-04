import { NextRequest, NextResponse } from 'next/server';
import { loginUser } from '@/lib/auth';
import { createSession } from '@/lib/session';
import { logSystemAction, getClientInfo } from '@/lib/logger';
import { saveUserSession } from '@/lib/auth-actions';
import { apiError } from '@/lib/api-error';
import { rateLimit, RATE_LIMITS, getRateLimitHeaders } from '@/lib/rate-limit';

export const dynamic = 'force-dynamic';

export async function POST(request: NextRequest) {
  const { ipAddress, userAgent } = getClientInfo(request);
  const ip = ipAddress || 'unknown';

  try {
    const body = await request.json();

    const { email, password, rememberMe } = body;

    if (!email || !password) {
      return apiError('Email e senha são obrigatórios', { request, status: 400 });
    }

    const normalizedEmail = String(email).toLowerCase().trim();

    // =============================================
    // RATE LIMITING (MEDIO-002): por IP (10/15min) e por e-mail (5/15min)
    // Aplicado ANTES de loginUser. Erro generico ('Muitas tentativas...').
    // =============================================
    const ipLimit = await rateLimit(
      `login:ip:${ip}`,
      RATE_LIMITS.LOGIN_IP.maxRequests,
      RATE_LIMITS.LOGIN_IP.windowMs,
    );

    if (!ipLimit.success) {
      console.warn('[LOGIN API] IP rate limit exceeded');
      return apiError('Muitas tentativas. Aguarde antes de tentar novamente.', {
        headers: getRateLimitHeaders(ipLimit),
        request,
        status: 429,
      });
    }

    const emailLimit = await rateLimit(
      `login:email:${normalizedEmail}`,
      RATE_LIMITS.LOGIN_EMAIL.maxRequests,
      RATE_LIMITS.LOGIN_EMAIL.windowMs,
    );

    if (!emailLimit.success) {
      console.warn('[LOGIN API] Email rate limit exceeded');
      return apiError('Muitas tentativas. Aguarde antes de tentar novamente.', {
        headers: getRateLimitHeaders(emailLimit),
        request,
        status: 429,
      });
    }

    const { user, company, error } = await loginUser(email, password);

    if (error || !user) {
      console.error('[LOGIN API] Login failed:', error);

      await logSystemAction({
        actionType: 'LOGIN_FAILED',
        details: { email, error },
        ipAddress,
        userAgent,
        status: 'error',
        errorMessage: error || 'Falha no login',
      });

      return apiError(error || 'Erro ao fazer login', { request, status: 401 });
    }

    // console.log('[LOGIN API] Login successful for:', email);

    // Criar dados da sessão
    const sessionData = createSession(user, rememberMe || false, company);

    await logSystemAction({
      userId: user.id,
      companyId: user.company_id || undefined,
      actionType: 'LOGIN_SUCCESS',
      resourceType: 'user',
      resourceId: user.id,
      details: {
        email: user.email,
        rememberMe,
        companyName: company?.company_name,
      },
      ipAddress,
      userAgent,
      sessionId: sessionData.userId,
      status: 'success',
    });

    await saveUserSession(sessionData, rememberMe || false);

    return NextResponse.json(
      {
        success: true,
        user: {
          id: user.id,
          email: user.email,
          firstName: user.first_name,
          lastName: user.last_name,
          planId: user.plan_id,
        },
      },
      { status: 200 },
    );
  } catch (error) {
    console.error('Login API error:', error);

    await logSystemAction({
      actionType: 'ERROR_OCCURRED',
      details: { error: String(error), endpoint: '/api/auth/login' },
      ipAddress,
      userAgent,
      status: 'error',
      errorMessage: 'Erro interno do servidor',
    });

    return apiError('Erro interno do servidor', {
      cause: error,
      logMessage: 'Login API error',
      request,
      status: 500,
    });
  }
}
