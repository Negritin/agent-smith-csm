import { NextRequest, NextResponse } from 'next/server';
import { auditCrossTenantAttempt, requireAdminSession } from '@/lib/auth-actions';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

const supabaseAdmin = getSupabaseAdmin();

async function enforceTargetUserTenant(params: {
  adminId: string;
  role: 'master_admin' | 'company_admin';
  companyId?: string | null;
  userId: string;
  action: string;
  request?: Request;
}): Promise<NextResponse | null> {
  if (params.role === 'master_admin') {
    return null;
  }

  const { data: targetUser, error } = await supabaseAdmin
    .from('users_v2')
    .select('company_id')
    .eq('id', params.userId)
    .single();

  if (error || !targetUser) {
    return NextResponse.json({ error: 'Usuário não encontrado' }, { status: 404 });
  }

  if (!params.companyId || targetUser.company_id !== params.companyId) {
    await auditCrossTenantAttempt({
      actorId: params.adminId,
      actorRole: params.role,
      actorCompanyId: params.companyId,
      resourceType: 'users_v2',
      resourceId: params.userId,
      targetCompanyId: targetUser.company_id,
      action: params.action,
      request: params.request,
    });

    return NextResponse.json({ error: 'Usuário não encontrado' }, { status: 404 });
  }

  return null;
}

/**
 * GET /api/admin/memory/user/[userId]
 * Busca memória de um usuário específico (fatos + resumos)
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ userId: string }> },
) {
  try {
    // Auth check
    const auth = await requireAdminSession();
    if (auth.response) return auth.response;

    const { userId } = await params;

    if (!userId) {
      return NextResponse.json({ error: 'userId is required' }, { status: 400 });
    }

    const tenantError = await enforceTargetUserTenant({
      adminId: auth.session.adminId,
      role: auth.session.role,
      companyId: auth.session.companyId,
      userId,
      action: 'read_user_memory',
      request,
    });
    if (tenantError) return tenantError;

    // Buscar fatos do usuário
    const { data: userMemory, error: memoryError } = await supabaseAdmin
      .from('user_memories')
      .select('*')
      .eq('user_id', userId)
      .single();

    // Buscar resumos de sessão
    const { data: sessionSummaries, error: summariesError } = await supabaseAdmin
      .from('session_summaries')
      .select('*')
      .eq('user_id', userId)
      .order('created_at', { ascending: false })
      .limit(10);

    return NextResponse.json({
      user_memory: userMemory || null,
      session_summaries: sessionSummaries || [],
      has_memory: !!userMemory,
      total_summaries: sessionSummaries?.length || 0,
    });
  } catch (error) {
    console.error('[Memory User] GET error:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}

/**
 * DELETE /api/admin/memory/user/[userId]
 * Apaga memória de um usuário (fatos + resumos)
 */
export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ userId: string }> },
) {
  try {
    // Auth check
    const auth = await requireAdminSession();
    if (auth.response) return auth.response;

    const { userId } = await params;

    if (!userId) {
      return NextResponse.json({ error: 'userId is required' }, { status: 400 });
    }

    const tenantError = await enforceTargetUserTenant({
      adminId: auth.session.adminId,
      role: auth.session.role,
      companyId: auth.session.companyId,
      userId,
      action: 'delete_user_memory',
      request,
    });
    if (tenantError) return tenantError;

    // Deletar fatos
    const { error: memoryError } = await supabaseAdmin
      .from('user_memories')
      .delete()
      .eq('user_id', userId);

    // Deletar resumos
    const { error: summariesError } = await supabaseAdmin
      .from('session_summaries')
      .delete()
      .eq('user_id', userId);

    if (memoryError || summariesError) {
      console.error('[Memory User] DELETE errors:', { memoryError, summariesError });
      return NextResponse.json({ error: 'Failed to delete user memory' }, { status: 500 });
    }

    return NextResponse.json({
      success: true,
      message: 'User memory deleted successfully',
    });
  } catch (error) {
    console.error('[Memory User DELETE error:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
