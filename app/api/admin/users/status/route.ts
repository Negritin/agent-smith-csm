import { NextRequest, NextResponse } from 'next/server';
import { apiError, authApiError } from '@/lib/api-error';
import { auditCrossTenantAttempt, requireAdminSession } from '@/lib/auth-actions';
import { logSecurityAudit } from '@/lib/security-audit';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

export const dynamic = 'force-dynamic';

const supabaseAdmin = getSupabaseAdmin();

type UserTenantCheck =
  | {
      response: NextResponse;
      user?: never;
    }
  | {
      response?: never;
      user: {
        company_id: string | null;
        status: string | null;
      };
    };

async function enforceUserTenant(params: {
  adminId: string;
  role: 'master_admin' | 'company_admin';
  companyId?: string | null;
  userId: string;
  action: string;
  request?: Request;
}): Promise<UserTenantCheck> {
  const { data: user, error } = await supabaseAdmin
    .from('users_v2')
    .select('company_id, status')
    .eq('id', params.userId)
    .single();

  if (error || !user) {
    return {
      response: apiError('Usuário não encontrado', { request: params.request, status: 404 }),
    };
  }

  if (params.role !== 'master_admin' && (!params.companyId || user.company_id !== params.companyId)) {
    await auditCrossTenantAttempt({
      actorId: params.adminId,
      actorRole: params.role,
      actorCompanyId: params.companyId,
      resourceType: 'users_v2',
      resourceId: params.userId,
      targetCompanyId: user.company_id,
      action: params.action,
      request: params.request,
    });

    return {
      response: apiError('Usuário não encontrado', { request: params.request, status: 404 }),
    };
  }

  return { user };
}

/**
 * PUT /api/admin/users/status
 * Updates user status (active, suspended)
 */
export async function PUT(request: NextRequest) {
  try {
    const auth = await requireAdminSession();
    if (auth.response) return authApiError(auth.response, { request });
    const { session } = auth;

    const body = await request.json();
    const { userId, status } = body;

    if (!userId || !status) {
      return apiError('userId and status are required', { request, status: 400 });
    }

    if (!['active', 'suspended'].includes(status)) {
      return apiError('Invalid status. Must be active or suspended', { request, status: 400 });
    }

    const tenantCheck = await enforceUserTenant({
      adminId: session.adminId,
      role: session.role,
      companyId: session.companyId,
      userId,
      action: 'update_status',
      request,
    });
    if (tenantCheck.response) return tenantCheck.response;

    const { error } = await supabaseAdmin
      .from('users_v2')
      .update({
        status,
        updated_at: new Date().toISOString(),
      })
      .eq('id', userId);

    if (error) {
      return apiError('Error updating user status', {
        cause: error,
        logMessage: '[USER STATUS API] Error updating user status',
        request,
        status: 500,
      });
    }

    await logSecurityAudit({
      action: 'user_status_changed',
      actorId: session.adminId,
      actorRole: session.role,
      companyId: tenantCheck.user.company_id || session.companyId || null,
      targetCompanyId: tenantCheck.user.company_id || null,
      resourceType: 'users_v2',
      resourceId: userId,
      request,
      status: 'success',
      details: {
        previousStatus: tenantCheck.user.status,
        newStatus: status,
        source: 'admin_users_status_put',
      },
    });

    return NextResponse.json({
      success: true,
      message: status === 'suspended' ? 'Usuário suspenso' : 'Usuário ativado',
    });
  } catch (error: unknown) {
    return apiError('Internal server error', {
      cause: error,
      logMessage: '[USER STATUS API] Error',
      request,
      status: 500,
    });
  }
}

/**
 * POST /api/admin/users/status
 * Approve or reject pending user (for master admin use)
 */
export async function POST(request: NextRequest) {
  try {
    const auth = await requireAdminSession();
    if (auth.response) return authApiError(auth.response, { request });
    const { session } = auth;

    const body = await request.json();
    const { userId, action, companyId } = body;

    if (!userId || !action) {
      return apiError('userId and action are required', { request, status: 400 });
    }

    const tenantCheck = await enforceUserTenant({
      adminId: session.adminId,
      role: session.role,
      companyId: session.companyId,
      userId,
      action,
      request,
    });
    if (tenantCheck.response) return tenantCheck.response;

    if (action === 'approve') {
      if (!companyId) {
        return apiError('companyId is required for approval', { request, status: 400 });
      }

      if (session.role !== 'master_admin' && companyId !== session.companyId) {
        await auditCrossTenantAttempt({
          actorId: session.adminId,
          actorRole: session.role,
          actorCompanyId: session.companyId,
          resourceType: 'users_v2',
          resourceId: userId,
          targetCompanyId: companyId,
          action: 'approve_to_company',
          request,
        });
        return apiError('Usuário não encontrado', { request, status: 404 });
      }

      const { error } = await supabaseAdmin
        .from('users_v2')
        .update({
          status: 'active',
          company_id: companyId,
          updated_at: new Date().toISOString(),
        })
        .eq('id', userId);

      if (error) {
        return apiError('Error approving user', {
          cause: error,
          logMessage: '[USER STATUS API] Approve error',
          request,
          status: 500,
        });
      }

      await logSecurityAudit({
        action: 'user_status_changed',
        actorId: session.adminId,
        actorRole: session.role,
        companyId,
        targetCompanyId: companyId,
        resourceType: 'users_v2',
        resourceId: userId,
        request,
        status: 'success',
        details: {
          previousStatus: tenantCheck.user.status,
          newStatus: 'active',
          source: 'admin_users_status_post_approve',
        },
      });

      return NextResponse.json({ success: true, message: 'Usuário aprovado' });
    }

    if (action === 'reject') {
      const { error } = await supabaseAdmin
        .from('users_v2')
        .update({
          status: 'suspended',
          updated_at: new Date().toISOString(),
        })
        .eq('id', userId);

      if (error) {
        return apiError('Error rejecting user', {
          cause: error,
          logMessage: '[USER STATUS API] Reject error',
          request,
          status: 500,
        });
      }

      await logSecurityAudit({
        action: 'user_status_changed',
        actorId: session.adminId,
        actorRole: session.role,
        companyId: tenantCheck.user.company_id || session.companyId || null,
        targetCompanyId: tenantCheck.user.company_id || null,
        resourceType: 'users_v2',
        resourceId: userId,
        request,
        status: 'success',
        details: {
          previousStatus: tenantCheck.user.status,
          newStatus: 'suspended',
          source: 'admin_users_status_post_reject',
        },
      });

      return NextResponse.json({ success: true, message: 'Usuário rejeitado' });
    }

    return apiError('Invalid action', { request, status: 400 });
  } catch (error: unknown) {
    return apiError('Internal server error', {
      cause: error,
      logMessage: '[USER STATUS API] Error',
      request,
      status: 500,
    });
  }
}
