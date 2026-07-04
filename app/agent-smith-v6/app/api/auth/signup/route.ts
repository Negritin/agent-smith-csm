import { NextRequest, NextResponse } from 'next/server';
import { createUser, SignupData, validatePasswordStrength } from '@/lib/auth';
import { createSession } from '@/lib/session';
import { logSystemAction, getClientInfo } from '@/lib/logger';
import { saveUserSession } from '@/lib/auth-actions';
import { apiError } from '@/lib/api-error';
import { getSupabaseAdmin } from '@/lib/supabase-admin';
import { rateLimit, RATE_LIMITS, getRateLimitHeaders } from '@/lib/rate-limit';

const supabaseAdmin = getSupabaseAdmin();

export async function POST(request: NextRequest) {
  const { ipAddress, userAgent } = getClientInfo(request);
  const ip = ipAddress || 'unknown';

  try {
    // =============================================
    // RATE LIMITING (MEDIO-003): por IP (5/hora) antes de qualquer trabalho de
    // DB. Erro generico ('Muitas tentativas...') — nao revela existencia de conta.
    // =============================================
    const ipLimit = await rateLimit(
      `signup:ip:${ip}`,
      RATE_LIMITS.SIGNUP_IP.maxRequests,
      RATE_LIMITS.SIGNUP_IP.windowMs,
    );

    if (!ipLimit.success) {
      console.warn('[SIGNUP API] IP rate limit exceeded');
      return apiError('Muitas tentativas. Aguarde antes de tentar novamente.', {
        headers: getRateLimitHeaders(ipLimit),
        request,
        status: 429,
      });
    }

    const body = await request.json();

    const inviteToken = body.inviteToken;
    let inviteData: any = null;

    // Se tem invite token, validar
    if (inviteToken) {
      const { data: invite, error: inviteError } = await supabaseAdmin
        .from('invites')
        .select(
          'id, company_id, role, is_owner_invite, email, name, max_uses, current_uses, expires_at',
        )
        .eq('token', inviteToken)
        .single();

      if (inviteError || !invite) {
        return apiError('Token de convite inválido', { request, status: 404 });
      }

      // Verificar expiração
      const expiresAt = new Date(invite.expires_at);
      if (expiresAt < new Date()) {
        return apiError('Token de convite expirado', { request, status: 410 });
      }

      // Verificar usos
      if (invite.current_uses >= invite.max_uses) {
        return apiError('Token de convite já foi utilizado', { request, status: 451 });
      }

      // Verificar email nominal (se especificado)
      if (invite.email) {
        const inviteEmail = invite.email.toLowerCase().trim();
        const userEmail = body.email.toLowerCase().trim();

        if (inviteEmail !== userEmail) {
          return apiError('Este convite é exclusivo para outro email', { request, status: 403 });
        }
      }

      inviteData = invite;
    }

    // Prepare signup data
    // If invite exists:
    // - Use invite's role (admin_company or member)
    // - Extract is_owner_invite flag
    // - ALL users start as 'pending' (require approval)
    const signupData: SignupData = {
      firstName: body.firstName,
      lastName: body.lastName,
      cpf: body.cpf,
      phone: body.phone,
      email: body.email,
      birthDate: body.birthDate,
      password: body.password,
      termsAccepted: body.termsAccepted,
      acceptedTermsVersion: body.acceptedTermsVersion || null,
      companyId: inviteData?.company_id,
      status: 'pending', // ✅ CHANGED: Everyone needs approval
      role: inviteData?.role || undefined,
      isOwner: inviteData?.is_owner_invite || false, // ✅ NEW: Extract owner flag
    };

    if (!signupData.termsAccepted) {
      return apiError('Você deve aceitar os termos e condições', { request, status: 400 });
    }

    if (
      !signupData.firstName ||
      !signupData.lastName ||
      !signupData.cpf ||
      !signupData.phone ||
      !signupData.email ||
      !signupData.birthDate ||
      !signupData.password
    ) {
      return apiError('Todos os campos são obrigatórios', { request, status: 400 });
    }

    const passwordValidation = validatePasswordStrength(signupData.password);
    if (!passwordValidation.valid) {
      return apiError('A senha não atende à política de segurança', { request, status: 400 });
    }

    const { user, error } = await createUser(signupData);

    if (error || !user) {
      await logSystemAction({
        actionType: 'SIGNUP',
        details: { email: signupData.email, error },
        ipAddress,
        userAgent,
        status: 'error',
        errorMessage: error || 'Erro ao criar usuário',
      });

      return apiError(error || 'Erro ao criar usuário', { request, status: 400 });
    }

    const session = createSession(user, false);

    // Se usou invite, incrementar contador
    if (inviteData) {
      await supabaseAdmin
        .from('invites')
        .update({ current_uses: inviteData.current_uses + 1 })
        .eq('id', inviteData.id);
    }

    await logSystemAction({
      userId: user.id,
      companyId: user.company_id || undefined,
      actionType: 'SIGNUP',
      resourceType: 'user',
      resourceId: user.id,
      details: {
        email: user.email,
        firstName: user.first_name,
        lastName: user.last_name,
        viaInvite: !!inviteData,
      },
      ipAddress,
      userAgent,
      sessionId: session.userId,
      status: 'success',
    });

    await saveUserSession(session);

    return NextResponse.json(
      {
        success: true,
        user: {
          id: user.id,
          email: user.email,
          firstName: user.first_name,
          lastName: user.last_name,
        },
      },
      { status: 201 },
    );
  } catch (error) {
    console.error('Signup API error:', error);

    await logSystemAction({
      actionType: 'ERROR_OCCURRED',
      details: { error: String(error), endpoint: '/api/auth/signup' },
      ipAddress,
      userAgent,
      status: 'error',
      errorMessage: 'Erro interno do servidor',
    });

    return apiError('Erro interno do servidor', {
      cause: error,
      logMessage: 'Signup API error',
      request,
      status: 500,
    });
  }
}
