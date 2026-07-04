import { NextRequest, NextResponse } from 'next/server';
import { apiError, authApiError } from '@/lib/api-error';
import { auditCrossTenantAttempt, getUserOrAdminSession } from '@/lib/auth-actions';
import { logSecurityAudit } from '@/lib/security-audit';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

export const dynamic = 'force-dynamic';

/**
 * PATCH /api/conversations/[id]
 *
 * Updates a conversation (title, updated_at, status).
 * Requires: smith_user_session OR smith_admin_session cookie
 */
export async function PATCH(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  try {
    const { id: conversationId } = await params;

    // =============================================
    // AUTHENTICATION CHECK (USER OR ADMIN)
    // =============================================
    const auth = await getUserOrAdminSession();
    if (auth.response) return authApiError(auth.response, { request });

    // =============================================
    // SERVICE ROLE CLIENT
    // =============================================
    const supabaseAdmin = getSupabaseAdmin();

    // =============================================
    // VALIDATE INPUT
    // =============================================
    const body = await request.json();
    const { title, status, updated_at, unread_count } = body;

    // SHIM (§8.1, D1): esta rota DEIXOU de aceitar `status`. Toda transição de
    // `conversations.status` passa pela RPC transacional única via as ações
    // explícitas em /api/admin/conversations/[id]/* (split-brain §24). `status`
    // no body é rejeitado com 400 (NUNCA update direto); demais campos seguem.
    if (status !== undefined) {
      console.warn('[CONVERSATIONS API] Rejected direct status write (use attendance actions)', {
        conversationId,
        requestedStatus: status,
      });
      return apiError(
        "Atualização de 'status' não é permitida aqui. Use as ações de atendimento (handoff/claim/return-to-ai/close/reopen).",
        { request, status: 400 },
      );
    }

    // Build update object (apenas title/unread_count/updated_at).
    const updateData: Record<string, any> = {};
    if (title !== undefined) updateData.title = title;
    if (unread_count !== undefined) updateData.unread_count = unread_count;
    if (updated_at !== undefined) updateData.updated_at = updated_at;
    else updateData.updated_at = new Date().toISOString();

    // =============================================
    // UPDATE CONVERSATION
    // =============================================
    let query = supabaseAdmin.from('conversations').update(updateData).eq('id', conversationId);

    if (auth.userSession) {
      query = query.eq('user_id', auth.userSession.userId);
    } else if (auth.adminSession?.role !== 'master_admin') {
      if (!auth.adminSession?.companyId) {
        return apiError('Conversa não encontrada', { request, status: 404 });
      }
      query = query.eq('company_id', auth.adminSession.companyId);
    }

    const { data, error } = await query.select().single();

    if (error) {
      return apiError('Conversa não encontrada', { request, status: 404 });
    }

    return NextResponse.json({ conversation: data });
  } catch (error: unknown) {
    return apiError('Erro interno ao atualizar conversa', {
      cause: error,
      logMessage: '[CONVERSATIONS API] PATCH error',
      request,
      status: 500,
    });
  }
}

/**
 * GET /api/conversations/[id]
 *
 * Gets a single conversation with messages.
 * Requires: smith_user_session OR smith_admin_session cookie
 */
export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  try {
    const { id: conversationId } = await params;

    // =============================================
    // AUTHENTICATION CHECK
    // =============================================
    const auth = await getUserOrAdminSession();
    if (auth.response) return authApiError(auth.response, { request });

    // =============================================
    // SERVICE ROLE CLIENT
    // =============================================
    const supabaseAdmin = getSupabaseAdmin();

    // =============================================
    // FETCH CONVERSATION WITH MESSAGES
    // =============================================
    let query = supabaseAdmin
      .from('conversations')
      .select('*, messages(*)')
      .eq('id', conversationId);

    if (auth.userSession) {
      query = query.eq('user_id', auth.userSession.userId);
    } else if (auth.adminSession?.role !== 'master_admin') {
      if (!auth.adminSession?.companyId) {
        return apiError('Conversa não encontrada', { request, status: 404 });
      }
      query = query.eq('company_id', auth.adminSession.companyId);
    }

    const { data: conversation, error } = await query.single();

    if (error) {
      return apiError('Conversa não encontrada', { request, status: 404 });
    }

    return NextResponse.json({ conversation });
  } catch (error: unknown) {
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[CONVERSATIONS API] GET error',
      request,
      status: 500,
    });
  }
}

/**
 * DELETE /api/conversations/[id]
 *
 * Deletes a conversation after explicit ownership checks.
 * Requires: smith_user_session OR smith_admin_session cookie
 */
export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const { id: conversationId } = await params;
    const auth = await getUserOrAdminSession();
    if (auth.response) return authApiError(auth.response, { request });

    const supabaseAdmin = getSupabaseAdmin();
    const { data: conversation, error: conversationError } = await supabaseAdmin
      .from('conversations')
      .select('id, company_id, user_id')
      .eq('id', conversationId)
      .single();

    if (conversationError || !conversation) {
      return apiError('Conversa não encontrada', { request, status: 404 });
    }

    if (auth.userSession && conversation.user_id !== auth.userSession.userId) {
      await logSecurityAudit({
        action: 'cross_tenant_attempt',
        actorId: auth.userSession.userId,
        actorRole: 'user',
        companyId: auth.userSession.companyId || null,
        targetCompanyId: conversation.company_id,
        resourceType: 'conversations',
        resourceId: conversationId,
        request,
        status: 'error',
        details: {
          attemptedAction: 'delete_conversation',
        },
      });

      return apiError('Conversa não encontrada', { request, status: 404 });
    }

    if (
      auth.adminSession &&
      auth.adminSession.role !== 'master_admin' &&
      conversation.company_id !== auth.adminSession.companyId
    ) {
      await auditCrossTenantAttempt({
        actorId: auth.adminSession.adminId,
        actorRole: auth.adminSession.role,
        actorCompanyId: auth.adminSession.companyId,
        resourceType: 'conversations',
        resourceId: conversationId,
        targetCompanyId: conversation.company_id,
        action: 'delete_conversation',
        request,
      });

      return apiError('Conversa não encontrada', { request, status: 404 });
    }

    const { error } = await supabaseAdmin.from('conversations').delete().eq('id', conversationId);

    if (error) {
      return apiError('Erro ao deletar conversa', {
        cause: error,
        logMessage: '[CONVERSATIONS API] DELETE error',
        request,
        status: 500,
      });
    }

    await logSecurityAudit({
      action: 'resource_deleted',
      actorId: auth.userSession?.userId || auth.adminSession?.adminId || null,
      actorRole: auth.userSession ? 'user' : auth.adminSession?.role || null,
      companyId: conversation.company_id,
      targetCompanyId: conversation.company_id,
      resourceType: 'conversations',
      resourceId: conversationId,
      request,
      status: 'success',
      details: {
        deletedResourceType: 'conversations',
      },
    });

    return NextResponse.json({ success: true });
  } catch (error: unknown) {
    return apiError('Erro interno ao deletar conversa', {
      cause: error,
      logMessage: '[CONVERSATIONS API] DELETE error',
      request,
      status: 500,
    });
  }
}
