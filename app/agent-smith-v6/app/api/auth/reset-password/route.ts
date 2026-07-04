import { NextRequest, NextResponse } from 'next/server';
import { createClient, type SupabaseClient } from '@supabase/supabase-js';
import { hashPassword, validatePasswordStrength, verifyResetToken } from '@/lib/auth';
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
 * POST /api/auth/reset-password
 *
 * SECURITY FEATURES:
 * - Rate limiting: 10 attempts/hour per IP, 5 per token
 * - Password strength validation (8+ chars, upper, lower, number)
 * - Token invalidation after 5 failed attempts
 * - Uses bcrypt for new passwords
 * - Generic error messages to prevent enumeration
 */
export async function POST(request: NextRequest) {
  const ip =
    request.headers.get('x-forwarded-for')?.split(',')[0] ||
    request.headers.get('x-real-ip') ||
    'unknown';

  try {
    const body = await request.json();
    const { email, code, newPassword } = body;

    // =============================================
    // INPUT VALIDATION
    // =============================================

    if (!email || !code || !newPassword) {
      return apiError('Dados incompletos', { request, status: 400 });
    }

    // Validate password strength
    const passwordValidation = validatePasswordStrength(newPassword);
    if (!passwordValidation.valid) {
      return apiError('Senha inválida', { request, status: 400 });
    }

    const normalizedEmail = email.toLowerCase().trim();
    const normalizedCode = code.toUpperCase().trim(); // Tokens are uppercase

    // =============================================
    // RATE LIMITING
    // =============================================

    // Check IP rate limit
    const ipLimit = await rateLimit(
      `reset:ip:${ip}`,
      RATE_LIMITS.RESET_PASSWORD_IP.maxRequests,
      RATE_LIMITS.RESET_PASSWORD_IP.windowMs,
    );

    if (!ipLimit.success) {
      log.warn('[RESET PASSWORD] IP rate limit exceeded', { ip });
      return apiError('Muitas tentativas. Aguarde antes de tentar novamente.', {
        headers: getRateLimitHeaders(ipLimit),
        request,
        status: 429,
      });
    }

    log.info('[RESET PASSWORD] Request received', { email: normalizedEmail });

    // =============================================
    // FIND USER AND VALIDATE TOKEN
    // =============================================

    // Try users_v2 first
    const { data: user, error: userError } = await supabaseAdmin
      .from('users_v2')
      .select('id, email, role, reset_token, reset_token_expires_at, reset_attempts')
      .ilike('email', normalizedEmail)
      .maybeSingle();

    if (!userError && user) {
      return await processReset(
        supabaseAdmin,
        'users_v2',
        user,
        normalizedCode,
        newPassword,
        ip,
        request,
      );
    }

    // Try admin_users (Master Admin)
    const { data: admin, error: adminError } = await supabaseAdmin
      .from('admin_users')
      .select('id, email, reset_token, reset_token_expires_at, reset_attempts')
      .ilike('email', normalizedEmail)
      .maybeSingle();

    if (!adminError && admin) {
      return await processReset(
        supabaseAdmin,
        'admin_users',
        admin,
        normalizedCode,
        newPassword,
        ip,
        request,
        true,
      );
    }

    // User not found - generic error
    log.info('[RESET PASSWORD] Email not found', { email: sanitizeEmail(normalizedEmail) });
    return apiError('Código inválido ou expirado. Solicite um novo.', { request, status: 400 });
  } catch (error) {
    log.error('[RESET PASSWORD] Critical error', {
      errorName: error instanceof Error ? error.name : typeof error,
    });
    return apiError('Erro interno ao processar solicitação', {
      cause: error,
      logMessage: '[RESET PASSWORD] Critical error',
      request,
      status: 500,
    });
  }
}

/**
 * Registro de reset carregado de `users_v2` ou `admin_users` (ALTO-006).
 *
 * `role` só existe em `users_v2` (admin_users é sempre Master Admin), por isso é
 * opcional. `reset_token` armazena o HASH do token (BAIXO-001), nunca o claro.
 */
interface ResetRecord {
  id: string;
  email: string;
  role?: string | null;
  reset_token: string | null;
  reset_attempts: number | null;
  reset_token_expires_at: string | null;
}

/**
 * Process password reset for user or admin
 */
async function processReset(
  supabaseClient: SupabaseClient,
  table: 'users_v2' | 'admin_users',
  record: ResetRecord,
  code: string,
  newPassword: string,
  ip: string,
  request: Request,
  isAdmin: boolean = false,
): Promise<NextResponse> {
  const maxAttempts = RATE_LIMITS.RESET_PASSWORD_TOKEN.maxRequests;
  const currentAttempts = record.reset_attempts || 0;

  // =============================================
  // CHECK IF TOKEN IS INVALIDATED (5+ failures)
  // =============================================

  if (currentAttempts >= maxAttempts) {
    log.warn('[RESET PASSWORD] Token invalidated due to too many attempts', {
      email: sanitizeEmail(record.email),
      attempts: currentAttempts,
    });
    return apiError('Token invalidado. Solicite um novo código.', { request, status: 400 });
  }

  // =============================================
  // VALIDATE TOKEN
  // =============================================

  // 🔒 BAIXO-001: compara o HASH do código informado contra o hash armazenado em
  // tempo constante (timingSafeEqual), em vez do antigo `!==`. Tokens legados em
  // texto puro não batem e ficam invalidados na transição (TTL de 15 min).
  if (!record.reset_token || !verifyResetToken(code, record.reset_token)) {
    // Increment failed attempts
    await supabaseClient
      .from(table)
      .update({ reset_attempts: currentAttempts + 1 })
      .eq('id', record.id);

    log.warn('[RESET PASSWORD] Invalid token', {
      email: sanitizeEmail(record.email),
      attempts: currentAttempts + 1,
    });

    const remaining = maxAttempts - (currentAttempts + 1);
    if (remaining <= 0) {
      return apiError('Token invalidado após muitas tentativas. Solicite um novo código.', {
        request,
        status: 400,
      });
    }

    return apiError('Código inválido ou expirado. Solicite um novo.', { request, status: 400 });
  }

  // =============================================
  // CHECK EXPIRATION
  // =============================================

  if (!record.reset_token_expires_at || new Date(record.reset_token_expires_at) < new Date()) {
    log.info('[RESET PASSWORD] Token expired', { email: sanitizeEmail(record.email) });
    return apiError('Código inválido ou expirado. Solicite um novo.', { request, status: 400 });
  }

  // =============================================
  // HASH NEW PASSWORD WITH BCRYPT
  // =============================================

  const newHash = await hashPassword(newPassword);

  // =============================================
  // UPDATE PASSWORD AND CLEAR TOKEN
  // =============================================

  const { error: updateError } = await supabaseClient
    .from(table)
    .update({
      password_hash: newHash,
      reset_token: null,
      reset_token_expires_at: null,
      reset_attempts: 0,
      password_migrated_at: new Date().toISOString(), // Mark as bcrypt
    })
    .eq('id', record.id);

  if (updateError) {
    log.error('[RESET PASSWORD] Error updating password', { errorName: updateError.name });
    return apiError('Erro ao atualizar senha', {
      cause: updateError,
      logMessage: '[RESET PASSWORD] Error updating password',
      request,
      status: 500,
    });
  }

  log.info('[RESET PASSWORD] ✅ Password reset successfully', {
    email: sanitizeEmail(record.email),
    hashType: 'bcrypt',
  });

  // Determine user type for redirect
  let userType = 'member';
  if (isAdmin || (record.role && ['admin_company', 'owner', 'admin'].includes(record.role))) {
    userType = 'admin';
  }

  return NextResponse.json({
    success: true,
    message: 'Senha alterada com sucesso!',
    userType,
  });
}
