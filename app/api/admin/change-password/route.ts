import { NextRequest } from 'next/server';
import { apiError, authApiError } from '@/lib/api-error';
import { changePassword, requireAdminSession } from '@/lib/auth-actions';

export const dynamic = 'force-dynamic';

/**
 * POST /api/admin/change-password
 *
 * EXCLUSIVE endpoint for Master Admin password change.
 * Only interacts with admin_users table.
 *
 * Security:
 * - Requires valid smith_admin_session cookie
 * - Only searches in admin_users table
 * - Never touches users_v2
 */
export async function POST(request: NextRequest) {
  try {
    const auth = await requireAdminSession();
    if (auth.response) return authApiError(auth.response, { request });

    // ========================================
    // PARSE REQUEST BODY
    // ========================================
    const body = await request.json();
    const { currentPassword, newPassword } = body;

    if (!currentPassword || !newPassword) {
      return apiError('Dados incompletos', { request, status: 400 });
    }

    return changePassword({
      table: 'admin_users',
      userId: auth.session.adminId,
      currentPassword,
      newPassword,
      notFoundMessage: 'Administrador não encontrado',
      request,
    });
  } catch (error: unknown) {
    return apiError('Erro interno ao processar troca de senha', {
      cause: error,
      logMessage: '[ADMIN CHANGE PASSWORD] Critical error',
      request,
      status: 500,
    });
  }
}
