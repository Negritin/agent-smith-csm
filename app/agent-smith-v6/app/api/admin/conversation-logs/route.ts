import { NextRequest, NextResponse } from 'next/server';
import { auditCrossTenantAttempt, requireAdminSession } from '@/lib/auth-actions';
import { auditMasterAdminCompanyOverride } from '@/lib/security-audit';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

const supabaseAdmin = getSupabaseAdmin();

/**
 * GET /api/admin/conversation-logs
 * Returns conversation logs with related data for the admin logs page.
 */
export async function GET(request: NextRequest) {
  try {
    const auth = await requireAdminSession();
    if (auth.response) return auth.response;
    const { session } = auth;

    const { searchParams } = new URL(request.url);
    const requestedCompanyId = searchParams.get('company_id');

    // Build query
    let query = supabaseAdmin
      .from('conversation_logs')
      .select('*')
      .order('created_at', { ascending: false })
      .limit(100);

    if (session.role === 'company_admin') {
      if (
        requestedCompanyId &&
        requestedCompanyId !== 'all' &&
        requestedCompanyId !== session.companyId
      ) {
        await auditCrossTenantAttempt({
          actorId: session.adminId,
          actorRole: session.role,
          actorCompanyId: session.companyId,
          resourceType: 'conversation_logs',
          resourceId: requestedCompanyId,
          targetCompanyId: requestedCompanyId,
          action: 'read_conversation_logs',
          request,
        });

        return NextResponse.json({ error: 'Logs não encontrados' }, { status: 404 });
      }

      query = query.eq('company_id', session.companyId);
    } else if (requestedCompanyId && requestedCompanyId !== 'all') {
      await auditMasterAdminCompanyOverride({
        request,
        actorId: session.adminId,
        sessionCompanyId: session.companyId || null,
        frontendCompanyId: requestedCompanyId,
        resourceType: 'conversation_logs',
        resourceId: requestedCompanyId,
        action: 'read_conversation_logs',
      });
      query = query.eq('company_id', requestedCompanyId);
    }

    const { data: logs, error } = await query;

    if (error) {
      console.error('[CONVERSATION LOGS API] Error:', error);
      return NextResponse.json({ error: 'Error fetching logs' }, { status: 500 });
    }

    return NextResponse.json({ logs: logs || [] });
  } catch (error: any) {
    console.error('[CONVERSATION LOGS API] Error:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
