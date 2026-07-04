import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { sendRecoveryEmail } from '@/lib/email';
import { generateSecureToken, hashResetToken } from '@/lib/auth';
import { rateLimit, RATE_LIMITS, getRateLimitHeaders } from '@/lib/rate-limit';
import { log, sanitizeEmail } from '@/lib/logger';
import { apiError } from '@/lib/api-error';

// Service Role Client
const supabaseAdmin = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY!,
  { auth: { persistSession: false } },
);

/**
 * POST /api/auth/forgot-password
 *
 * SECURITY FEATURES:
 * - Rate limiting: 5 req/hour per IP, 3 req/hour per email
 * - Secure tokens via crypto.randomBytes (16 bytes / 128-bit entropy, hex-encoded); only the SHA-256 hash is stored
 * - Timing attack prevention (random delay)
 * - Enumeration prevention (same response for any email)
 *
 * Always returns 200 to prevent user enumeration.
 */
export async function POST(request: NextRequest) {
  // Get client IP for rate limiting
  const ip =
    request.headers.get('x-forwarded-for')?.split(',')[0] ||
    request.headers.get('x-real-ip') ||
    'unknown';

  try {
    const body = await request.json();
    const { email } = body;

    if (!email || !email.includes('@')) {
      return apiError('Email inválido', { request, status: 400 });
    }

    const normalizedEmail = email.toLowerCase().trim();

    // =============================================
    // RATE LIMITING
    // =============================================

    // Check IP rate limit
    const ipLimit = await rateLimit(
      `forgot:ip:${ip}`,
      RATE_LIMITS.FORGOT_PASSWORD_IP.maxRequests,
      RATE_LIMITS.FORGOT_PASSWORD_IP.windowMs,
    );

    if (!ipLimit.success) {
      log.warn('[FORGOT PASSWORD] Rate limit exceeded', { ip, email: normalizedEmail });
      return apiError('Muitas tentativas. Aguarde antes de tentar novamente.', {
        headers: getRateLimitHeaders(ipLimit),
        request,
        status: 429,
      });
    }

    // Check email rate limit
    const emailLimit = await rateLimit(
      `forgot:email:${normalizedEmail}`,
      RATE_LIMITS.FORGOT_PASSWORD_EMAIL.maxRequests,
      RATE_LIMITS.FORGOT_PASSWORD_EMAIL.windowMs,
    );

    if (!emailLimit.success) {
      log.warn('[FORGOT PASSWORD] Email rate limit exceeded', { email: normalizedEmail });
      return apiError('Muitas tentativas para este email. Aguarde antes de tentar novamente.', {
        headers: getRateLimitHeaders(emailLimit),
        request,
        status: 429,
      });
    }

    log.info('[FORGOT PASSWORD] Request received', { email: normalizedEmail });

    // =============================================
    // TIMING ATTACK PREVENTION
    // =============================================

    // Add random delay to prevent timing attacks
    const randomDelay = Math.floor(Math.random() * 200) + 100; // 100-300ms
    await new Promise((resolve) => setTimeout(resolve, randomDelay));

    // =============================================
    // GENERATE SECURE TOKEN
    // =============================================

    // 🔒 BAIXO-001: token com 128 bits de entropia (crypto.randomBytes). O valor
    // CLARO (`code`) é enviado SOMENTE ao usuário por e-mail; em repouso guardamos
    // apenas o hash (`hashResetToken`), comparado em tempo constante no reset.
    const code = generateSecureToken();
    const tokenHash = hashResetToken(code);
    const expiresAt = new Date(Date.now() + 15 * 60 * 1000).toISOString(); // 15 minutes

    // =============================================
    // PROCESS REQUEST (users_v2 first, then admin_users)
    // =============================================

    // Try users_v2 first
    const { data: user } = await supabaseAdmin
      .from('users_v2')
      .select('id, email')
      .ilike('email', normalizedEmail)
      .maybeSingle();

    if (user) {
      log.info('[FORGOT PASSWORD] User found', { email: sanitizeEmail(user.email) });

      // Update reset token and reset attempts counter
      const { error: updateError } = await supabaseAdmin
        .from('users_v2')
        .update({
          reset_token: tokenHash, // hash em repouso (nunca o token claro)
          reset_token_expires_at: expiresAt,
          reset_attempts: 0, // Reset counter for new token
        })
        .eq('id', user.id);

      if (!updateError) {
        log.info('[FORGOT PASSWORD] Token saved, sending email');
        await sendRecoveryEmail(user.email, code);
      } else {
        log.error('[FORGOT PASSWORD] Error saving token', { error: updateError.message });
      }
    } else {
      // Try admin_users (Master Admin)
      const { data: admin } = await supabaseAdmin
        .from('admin_users')
        .select('id, email')
        .ilike('email', normalizedEmail)
        .maybeSingle();

      if (admin) {
        log.info('[FORGOT PASSWORD] Admin found', { email: sanitizeEmail(admin.email) });

        const { error: updateError } = await supabaseAdmin
          .from('admin_users')
          .update({
            reset_token: tokenHash, // hash em repouso (nunca o token claro)
            reset_token_expires_at: expiresAt,
            reset_attempts: 0,
          })
          .eq('id', admin.id);

        if (!updateError) {
          log.info('[FORGOT PASSWORD] Admin token saved, sending email');
          await sendRecoveryEmail(admin.email, code);
        }
      } else {
        // User not found - log but return success (security)
        log.info('[FORGOT PASSWORD] Email not found (returning success anyway)', {
          email: sanitizeEmail(normalizedEmail),
        });
      }
    }

    // =============================================
    // ALWAYS RETURN SAME SUCCESS MESSAGE
    // =============================================

    return NextResponse.json(
      {
        success: true,
        message: 'Se este email estiver cadastrado, você receberá as instruções de recuperação.',
      },
      {
        headers: getRateLimitHeaders(ipLimit),
      },
    );
  } catch (error) {
    log.error('[FORGOT PASSWORD] Critical error', {
      errorName: error instanceof Error ? error.name : typeof error,
    });
    return apiError('Erro interno ao processar solicitação', {
      cause: error,
      logMessage: '[FORGOT PASSWORD] Critical error',
      request,
      status: 500,
    });
  }
}
