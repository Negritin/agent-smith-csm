import { NextRequest, NextResponse } from 'next/server';
import { apiError, authApiError } from '@/lib/api-error';
import { auditCrossTenantAttempt, requireAdminSession } from '@/lib/auth-actions';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

// Default settings
const DEFAULT_SETTINGS = {
  whatsapp_summarization_mode: 'sliding_window',
  whatsapp_sliding_window_size: 20,
  whatsapp_message_threshold: 30,
  web_summarization_mode: 'session_end',
  web_message_threshold: 20,
  extract_user_profile: true,
  extract_session_summary: true,
};

const supabaseAdmin = getSupabaseAdmin();

async function enforceAgentTenant(params: {
  adminId: string;
  role: 'master_admin' | 'company_admin';
  companyId?: string | null;
  agentId: string;
  action: string;
  request?: Request;
}): Promise<NextResponse | null> {
  const { data: agent, error } = await supabaseAdmin
    .from('agents')
    .select('company_id')
    .eq('id', params.agentId)
    .single();

  if (error || !agent) {
    return apiError('Agente não encontrado', { request: params.request, status: 404 });
  }

  if (params.role !== 'master_admin' && (!params.companyId || agent.company_id !== params.companyId)) {
    await auditCrossTenantAttempt({
      actorId: params.adminId,
      actorRole: params.role,
      actorCompanyId: params.companyId,
      resourceType: 'agents',
      resourceId: params.agentId,
      targetCompanyId: agent.company_id,
      action: params.action,
      request: params.request,
    });

    return apiError('Agente não encontrado', { request: params.request, status: 404 });
  }

  return null;
}

/**
 * GET /api/admin/memory/settings?agentId={id}
 * Busca configurações de memória do agente
 */
export async function GET(request: NextRequest) {
  try {
    // Auth check
    const auth = await requireAdminSession();
    if (auth.response) return authApiError(auth.response, { request });

    const { searchParams } = new URL(request.url);
    const agentId = searchParams.get('agentId');

    if (!agentId) {
      return apiError('agentId is required', { request, status: 400 });
    }

    const tenantError = await enforceAgentTenant({
      adminId: auth.session.adminId,
      role: auth.session.role,
      companyId: auth.session.companyId,
      agentId,
      action: 'read_memory_settings',
      request,
    });
    if (tenantError) return tenantError;

    // Buscar configuração existente por agent_id
    const { data, error } = await supabaseAdmin
      .from('memory_settings')
      .select('*')
      .eq('agent_id', agentId)
      .single();

    // Se não existir, criar default para o agente
    if (error || !data) {
      const { data: newData, error: insertError } = await supabaseAdmin
        .from('memory_settings')
        .insert({
          agent_id: agentId,
          ...DEFAULT_SETTINGS,
        })
        .select()
        .single();

      if (insertError) {
        return apiError('Failed to create default settings', {
          cause: insertError,
          logMessage: '[Memory Settings] Error creating default',
          request,
          status: 500,
        });
      }

      return NextResponse.json(newData);
    }

    return NextResponse.json(data);
  } catch (error: unknown) {
    return apiError('Internal server error', {
      cause: error,
      logMessage: '[Memory Settings] GET error',
      request,
      status: 500,
    });
  }
}

/**
 * PUT /api/admin/memory/settings
 * Atualiza configurações de memória do agente
 */
export async function PUT(request: NextRequest) {
  try {
    // Auth check
    const auth = await requireAdminSession();
    if (auth.response) return authApiError(auth.response, { request });

    const body = await request.json();
    const {
      agentId,
      whatsapp_summarization_mode,
      whatsapp_sliding_window_size,
      whatsapp_message_threshold,
      web_summarization_mode,
      web_message_threshold,
      extract_user_profile,
      extract_session_summary,
    } = body;

    if (!agentId) {
      return apiError('agentId is required', { request, status: 400 });
    }

    const tenantError = await enforceAgentTenant({
      adminId: auth.session.adminId,
      role: auth.session.role,
      companyId: auth.session.companyId,
      agentId,
      action: 'update_memory_settings',
      request,
    });
    if (tenantError) return tenantError;

    // Upsert (insert or update) por agent_id
    const { data, error } = await supabaseAdmin
      .from('memory_settings')
      .upsert(
        {
          agent_id: agentId,
          whatsapp_summarization_mode,
          whatsapp_sliding_window_size,
          whatsapp_message_threshold,
          web_summarization_mode,
          web_message_threshold,
          extract_user_profile,
          extract_session_summary,
          updated_at: new Date().toISOString(),
        },
        {
          onConflict: 'agent_id',
        },
      )
      .select()
      .single();

    if (error) {
      return apiError('Failed to update settings', {
        cause: error,
        logMessage: '[Memory Settings] PUT error',
        request,
        status: 500,
      });
    }

    return NextResponse.json(data);
  } catch (error: unknown) {
    return apiError('Internal server error', {
      cause: error,
      logMessage: '[Memory Settings] PUT error',
      request,
      status: 500,
    });
  }
}
