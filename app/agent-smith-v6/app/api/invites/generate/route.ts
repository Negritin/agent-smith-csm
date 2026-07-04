import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { randomBytes } from 'crypto';
import { apiError, authApiError } from '@/lib/api-error';
import { sendInviteEmail } from '@/lib/email';
import { auditCrossTenantAttempt, requireAdminSession } from '@/lib/auth-actions';
import { errorLogFields, log, sanitizeEmail } from '@/lib/logger';

export const dynamic = 'force-dynamic';

// Service Role Client
const supabaseAdmin = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY!,
  { auth: { persistSession: false } },
);

/**
 * POST /api/invites/generate
 *
 * Generate a new invite token and send email
 * Master Admin: Can set any role and any companyId
 * Company Admin: Can set role but companyId is forced to their company
 */
export async function POST(request: NextRequest) {
  try {
    const auth = await requireAdminSession();
    if (auth.response) return authApiError(auth.response, { request });

    const session = auth.session;
    const body = await request.json();
    const { role: inviteRole, companyId: requestedCompanyId, email, name, isOwner } = body;

    let companyAdminSessionCompanyId: string | null = null;
    if (session.role === 'company_admin') {
      const sessionCompanyId = session.companyId;
      if (!sessionCompanyId) {
        return apiError('Não autorizado', { request, status: 403 });
      }

      companyAdminSessionCompanyId = sessionCompanyId;

      if (requestedCompanyId && requestedCompanyId !== sessionCompanyId) {
        await auditCrossTenantAttempt({
          actorId: session.adminId,
          actorRole: session.role,
          actorCompanyId: sessionCompanyId,
          resourceType: 'company',
          resourceId: requestedCompanyId,
          targetCompanyId: requestedCompanyId,
          action: 'invite.generate',
          request,
        });
        return apiError('Não encontrado', { request, status: 404 });
      }
    }

    // Validate role
    if (inviteRole && !['admin_company', 'member'].includes(inviteRole)) {
      return apiError('Invalid role. Must be admin_company or member', { request, status: 400 });
    }

    // Email is required for nominal invites
    if (!email || typeof email !== 'string' || !email.includes('@')) {
      return apiError('Valid email is required for nominal invites', { request, status: 400 });
    }

    const normalizedEmail = email.toLowerCase().trim();

    let finalCompanyId: string;
    let finalRole: string = inviteRole || 'member';
    let companyName = '';

    if (session.role === 'master_admin') {
      // Master Admin can set any company and any role
      if (!requestedCompanyId) {
        return apiError('Master admin must specify companyId', { request, status: 400 });
      }

      finalCompanyId = requestedCompanyId;

      // Get company info including max_users for limit validation
      const { data: company } = await supabaseAdmin
        .from('companies')
        .select('company_name, max_users')
        .eq('id', finalCompanyId)
        .single();

      companyName = company?.company_name || 'Empresa';
      const maxAdmins = company?.max_users || 5;

      // VALIDATION: Check admin limit for admin_company invites
      if (inviteRole === 'admin_company') {
        const { count: adminCount } = await supabaseAdmin
          .from('users_v2')
          .select('*', { count: 'exact', head: true })
          .eq('company_id', finalCompanyId)
          .in('role', ['admin_company', 'owner', 'admin'])
          .neq('status', 'suspended');

        if ((adminCount || 0) >= maxAdmins) {
          return apiError(`Limite de ${maxAdmins} administradores atingido para esta empresa`, {
            request,
            status: 403,
          });
        }
      }

      // VALIDATION: Only Master can create Owner
      if (isOwner && inviteRole === 'admin_company') {
        log.debug('[INVITE GENERATE] Master creating Admin Company Owner', {
          companyId: finalCompanyId,
          role: finalRole,
        });
      } else if (isOwner && inviteRole === 'member') {
        return apiError('Members cannot be owners', { request, status: 400 });
      }

      log.debug('[INVITE GENERATE] Master admin generating nominal invite', {
        companyId: finalCompanyId,
        role: finalRole,
        isOwner: isOwner || false,
        email: sanitizeEmail(normalizedEmail),
      });
    } else {
      // Company Admin - force their company and reject cross-tenant hints.
      const sessionCompanyId = companyAdminSessionCompanyId;
      if (!sessionCompanyId) {
        return apiError('Não autorizado', { request, status: 403 });
      }

      // Get user and company info with is_owner
      const { data: user, error: userError } = await supabaseAdmin
        .from('users_v2')
        .select('id, company_id, role, is_owner, companies:company_id(company_name)')
        .eq('id', session.adminId)
        .single();

      if (userError || !user || user.company_id !== sessionCompanyId) {
        return apiError('User not found', { request, status: 404 });
      }

      // Check if user is admin_company
      if (user.role !== 'admin_company') {
        return apiError('Only company admins can generate invites', { request, status: 403 });
      }

      // VALIDATION 1: Only Owner can create Admin Company
      if (inviteRole === 'admin_company' && !user.is_owner) {
        return apiError('Não autorizado', { request, status: 403 });
      }

      // VALIDATION 2: Company Admin cannot create Owner (only Master can)
      if (isOwner) {
        return apiError('Não autorizado', { request, status: 403 });
      }

      // Force company to user's company
      finalCompanyId = sessionCompanyId;
      companyName = (user.companies as any)?.company_name || 'Empresa';

      // VALIDATION 3: Check admin limit for admin_company invites
      if (inviteRole === 'admin_company') {
        const { data: companyData } = await supabaseAdmin
          .from('companies')
          .select('max_users')
          .eq('id', finalCompanyId)
          .single();

        const maxAdmins = companyData?.max_users || 5;

        const { count: adminCount } = await supabaseAdmin
          .from('users_v2')
          .select('*', { count: 'exact', head: true })
          .eq('company_id', finalCompanyId)
          .in('role', ['admin_company', 'owner', 'admin'])
          .neq('status', 'suspended');

        if ((adminCount || 0) >= maxAdmins) {
          return apiError(`Limite de ${maxAdmins} administradores atingido para esta empresa`, {
            request,
            status: 403,
          });
        }
      }

      log.debug('[INVITE GENERATE] Company admin generating nominal invite', {
        companyId: finalCompanyId,
        role: finalRole,
        isOwner: false, // Company Admin can never create owners
        isAdminOwner: user.is_owner,
        email: sanitizeEmail(normalizedEmail),
        userId: session.adminId,
      });
    }

    // ✅ VALIDATION: Check if email already exists after auth and tenant authorization.
    const { data: existingUser, error: existingUserError } = await supabaseAdmin
      .from('users_v2')
      .select('id, email')
      .eq('email', normalizedEmail)
      .maybeSingle();

    if (existingUser) {
      return apiError('Este e-mail já está cadastrado no sistema', { request, status: 409 });
    }

    if (existingUserError && existingUserError.code !== 'PGRST116') {
      // PGRST116 = no rows returned, which is what we want
      log.warn('[INVITE GENERATE] Error checking existing email', {
        ...errorLogFields(existingUserError),
        email: sanitizeEmail(normalizedEmail),
      });
    }

    // Generate unique token
    const token = randomBytes(32).toString('hex');

    // Calculate expiration (7 days from now)
    const expiresAt = new Date();
    expiresAt.setDate(expiresAt.getDate() + 7);

    // Nominal invites have max_uses = 1 (one person only)
    const maxUses = 1;

    const calculatedIsOwner = (isOwner && inviteRole === 'admin_company') || false;
    log.debug('[INVITE GENERATE] Owner flag calculated', {
      isOwnerReceived: isOwner,
      inviteRole,
      finalRole,
      calculatedIsOwner,
      willSaveAsOwner: calculatedIsOwner,
    });

    // Insert invite with email, name, and owner flag
    const { data: invite, error: inviteError } = await supabaseAdmin
      .from('invites')
      .insert({
        company_id: finalCompanyId,
        token,
        role: finalRole,
        is_owner_invite: calculatedIsOwner, // Use calculated value
        email: email.toLowerCase().trim(),
        name: name || null,
        created_by: session.role === 'master_admin' ? null : session.adminId,
        max_uses: maxUses,
        current_uses: 0,
        expires_at: expiresAt.toISOString(),
      })
      .select()
      .single();

    if (inviteError || !invite) {
      return apiError('Failed to generate invite', {
        cause: inviteError,
        logMessage: '[INVITE GENERATE] Error',
        request,
        status: 500,
      });
    }

    log.debug('[INVITE GENERATE] Invite saved', {
      inviteId: invite.id,
      isOwnerInvite: invite.is_owner_invite,
    });

    // Build invite link
    const baseUrl = process.env.NEXT_PUBLIC_BASE_URL || 'http://localhost:3000';
    const inviteLink = `${baseUrl}/register?token=${token}`;

    // Send email
    let emailWarning = null;
    const emailResult = await sendInviteEmail({
      to: email.toLowerCase().trim(),
      name: name || undefined,
      inviteLink,
      role: finalRole as 'admin_company' | 'member',
      companyName,
    });

    if (!emailResult.success) {
      log.warn('[INVITE GENERATE] Email failed', {
        email: sanitizeEmail(normalizedEmail),
        error: emailResult.error,
      });
      emailWarning =
        'Convite criado, mas o email não pôde ser enviado. Compartilhe o link manualmente.';
    }

    return NextResponse.json({
      success: true,
      token,
      inviteLink,
      role: finalRole,
      email: email.toLowerCase().trim(),
      name: name || null,
      expiresAt: invite.expires_at,
      maxUses: invite.max_uses,
      emailSent: emailResult.success,
      warning: emailWarning,
    });
  } catch (error: unknown) {
    return apiError('Internal server error', {
      cause: error,
      logMessage: '[INVITE GENERATE] Error',
      request,
      status: 500,
    });
  }
}
