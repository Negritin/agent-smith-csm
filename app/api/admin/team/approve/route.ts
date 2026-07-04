import { NextRequest, NextResponse } from 'next/server';
import { apiError, authApiError } from '@/lib/api-error';
import { auditCrossTenantAttempt, requireAdminSession } from '@/lib/auth-actions';
import { logSecurityAudit } from '@/lib/security-audit';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

export const dynamic = 'force-dynamic';

const supabaseAdmin = getSupabaseAdmin();

/**
 * POST /api/admin/team/approve
 *
 * Approve a pending user
 * Requires: Company admin authentication
 */
export async function POST(request: NextRequest) {
  try {
    const auth = await requireAdminSession();
    if (auth.response) return authApiError(auth.response, { request });
    const { session } = auth;

    const { userId } = await request.json();

    if (!userId) {
      return apiError('User ID is required', { request, status: 400 });
    }

    // Get user to approve and verify same company
    const { data: user, error: userError } = await supabaseAdmin
      .from('users_v2')
      .select('id, company_id, status, email, first_name, role, is_owner')
      .eq('id', userId)
      .single();

    if (userError || !user) {
      return apiError('User not found', { request, status: 404 });
    }

    // Verify same company
    if (session.role !== 'master_admin' && user.company_id !== session.companyId) {
      await auditCrossTenantAttempt({
        actorId: session.adminId,
        actorRole: session.role,
        actorCompanyId: session.companyId,
        resourceType: 'users_v2',
        resourceId: userId,
        targetCompanyId: user.company_id,
        action: 'approve_team_member',
        request,
      });

      return apiError('User not found', { request, status: 404 });
    }

    // VALIDATION: Only Master Admin can approve Owners
    if (user.is_owner && user.role === 'admin_company' && session.role !== 'master_admin') {
      return apiError('Apenas Master Admin pode aprovar Admin Company Owner', {
        request,
        status: 403,
      });
    }

    // Update user status to active
    const { error: updateError } = await supabaseAdmin
      .from('users_v2')
      .update({ status: 'active' })
      .eq('id', userId);

    if (updateError) {
      return apiError('Failed to approve user', {
        cause: updateError,
        logMessage: '[APPROVE USER] Error',
        request,
        status: 500,
      });
    }

    await logSecurityAudit({
      action: 'user_status_changed',
      actorId: session.adminId,
      actorRole: session.role,
      companyId: user.company_id,
      targetCompanyId: user.company_id,
      resourceType: 'users_v2',
      resourceId: userId,
      request,
      status: 'success',
      details: {
        previousStatus: user.status,
        newStatus: 'active',
        source: 'admin_team_approve',
      },
    });

    return NextResponse.json({
      success: true,
      message: `User ${user.first_name} approved successfully`,
    });
  } catch (error: unknown) {
    return apiError('Internal server error', {
      cause: error,
      logMessage: '[APPROVE USER] Error',
      request,
      status: 500,
    });
  }
}
