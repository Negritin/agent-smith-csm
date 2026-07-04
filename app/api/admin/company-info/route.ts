import { NextRequest, NextResponse } from 'next/server';
import { apiError, authApiError } from '@/lib/api-error';
import { auditCrossTenantAttempt, requireAdminSession } from '@/lib/auth-actions';
import { auditMasterAdminCompanyOverride } from '@/lib/security-audit';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

const supabaseAdmin = getSupabaseAdmin();

/**
 * GET /api/admin/company-info?companyId=xxx
 * Fetches company info by ID using Service Role key
 */
export async function GET(request: NextRequest) {
  try {
    const auth = await requireAdminSession();
    if (auth.response) return authApiError(auth.response, { request });
    const { session } = auth;

    const { searchParams } = new URL(request.url);
    const requestedCompanyId = searchParams.get('companyId');
    const companyId = session.role === 'company_admin' ? session.companyId : requestedCompanyId;

    if (!companyId) {
      return apiError('companyId is required', { request, status: 400 });
    }

    if (
      session.role === 'company_admin' &&
      requestedCompanyId &&
      requestedCompanyId !== session.companyId
    ) {
      await auditCrossTenantAttempt({
        actorId: session.adminId,
        actorRole: session.role,
        actorCompanyId: session.companyId,
        resourceType: 'companies',
        resourceId: requestedCompanyId,
        targetCompanyId: requestedCompanyId,
        action: 'read_company_info',
        request,
      });

      return apiError('Empresa não encontrada', { request, status: 404 });
    }

    if (session.role === 'master_admin' && requestedCompanyId) {
      await auditMasterAdminCompanyOverride({
        request,
        actorId: session.adminId,
        sessionCompanyId: session.companyId || null,
        frontendCompanyId: requestedCompanyId,
        resourceType: 'companies',
        resourceId: requestedCompanyId,
        action: 'read_company_info',
      });
    }

    const { data, error } = await supabaseAdmin
      .from('companies')
      .select('id, company_name')
      .eq('id', companyId)
      .single();

    if (error) {
      return apiError('Erro ao buscar empresa', {
        cause: error,
        logMessage: '[ADMIN COMPANY-INFO] Error',
        request,
        status: 500,
      });
    }

    return NextResponse.json(data);
  } catch (error: unknown) {
    return apiError('Internal server error', {
      cause: error,
      logMessage: '[ADMIN COMPANY-INFO] Error',
      request,
      status: 500,
    });
  }
}
