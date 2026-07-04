import { NextRequest, NextResponse } from 'next/server';
import { apiError, authApiError } from '@/lib/api-error';
import { auditCrossTenantAttempt, requireAdminSession } from '@/lib/auth-actions';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

const supabaseAdmin = getSupabaseAdmin();

/**
 * PUT /api/admin/profile
 * Updates admin profile (first_name, last_name, avatar_url)
 */
export async function PUT(request: NextRequest) {
  try {
    const auth = await requireAdminSession();
    if (auth.response) return authApiError(auth.response, { request });
    const { session } = auth;

    const body = await request.json();
    const { userId, first_name, last_name, avatar_url } = body;
    const targetUserId = userId || (session.role === 'company_admin' ? session.adminId : null);

    if (!targetUserId) {
      return apiError('userId is required', { request, status: 400 });
    }

    if (session.role !== 'master_admin') {
      const { data: targetUser, error: targetError } = await supabaseAdmin
        .from('users_v2')
        .select('company_id')
        .eq('id', targetUserId)
        .single();

      if (targetError || !targetUser) {
        return apiError('Usuário não encontrado', { request, status: 404 });
      }

      if (targetUser.company_id !== session.companyId) {
        await auditCrossTenantAttempt({
          actorId: session.adminId,
          actorRole: session.role,
          actorCompanyId: session.companyId,
          resourceType: 'users_v2',
          resourceId: targetUserId,
          targetCompanyId: targetUser.company_id,
          action: 'update_profile',
          request,
        });

        return apiError('Usuário não encontrado', { request, status: 404 });
      }
    }

    const { error } = await supabaseAdmin
      .from('users_v2')
      .update({
        first_name,
        last_name,
        avatar_url,
      })
      .eq('id', targetUserId);

    if (error) {
      return apiError('Erro ao atualizar perfil', {
        cause: error,
        logMessage: '[ADMIN PROFILE] Update error',
        request,
        status: 500,
      });
    }

    return NextResponse.json({ success: true });
  } catch (error: unknown) {
    return apiError('Internal server error', {
      cause: error,
      logMessage: '[ADMIN PROFILE] Error',
      request,
      status: 500,
    });
  }
}
