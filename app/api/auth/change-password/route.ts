import { NextRequest } from 'next/server';
import { apiError, authApiError } from '@/lib/api-error';
import { changePassword, requireUserSession } from '@/lib/auth-actions';

/**
 * POST /api/auth/change-password
 *
 * Standard password change for users_v2 table ONLY.
 * Used by: Regular Members, Company Admins
 *
 * Master Admin should use /api/admin/change-password instead.
 */
export async function POST(request: NextRequest) {
  try {
    const auth = await requireUserSession();
    if (auth.response) return authApiError(auth.response, { request });

    const body = await request.json();
    const { userId, currentPassword, newPassword } = body;

    if (!currentPassword || !newPassword) {
      return apiError('Dados incompletos', { request, status: 400 });
    }

    if (userId && userId !== auth.session.userId) {
      return apiError('Não autorizado', { request, status: 403 });
    }

    return changePassword({
      table: 'users_v2',
      userId: auth.session.userId,
      currentPassword,
      newPassword,
      notFoundMessage: 'Usuário não encontrado',
      request,
    });
  } catch (error: unknown) {
    return apiError('Erro interno ao processar troca de senha', {
      cause: error,
      logMessage: '[CHANGE PASSWORD] Critical error',
      request,
      status: 500,
    });
  }
}
