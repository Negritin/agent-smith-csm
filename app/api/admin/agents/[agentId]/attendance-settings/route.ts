import { NextRequest, NextResponse } from 'next/server';
import { apiError, authApiError } from '@/lib/api-error';
import {
  type AdminSession,
  auditCrossTenantAttempt,
  requireAdminSession,
} from '@/lib/auth-actions';
import { log } from '@/lib/logger';
import { getSupabaseAdmin } from '@/lib/supabase-admin';
import { mergeAttendanceToolsConfig } from '@/lib/tools-config-merge';

export const dynamic = 'force-dynamic';

const supabaseAdmin = getSupabaseAdmin();

/**
 * GET/PATCH /api/admin/agents/[agentId]/attendance-settings (§9.3, §7.7).
 *
 * `agent_attendance_settings` é a FONTE DE VERDADE das flags POR AGENTE: handoff,
 * reabertura e encerramento pelo agente. O encerramento automático por
 * inatividade (auto_close_*) NÃO mora mais aqui — virou config da EMPRESA
 * (`company_attendance_settings`, §16), gerida por /api/admin/company/
 * attendance-settings. Esta rota NÃO lê nem grava `auto_close_*`.
 *
 * GET retorna defaults mesmo sem registro. PATCH grava a tabela E faz DEEP-MERGE
 * em `agents.tools_config` atualizando APENAS os espelhos `human_handoff.enabled`
 * (de handoff_enabled) e `end_attendance.enabled` (de agent_can_close),
 * preservando `csv_analytics` e chaves desconhecidas, bumpando `agents.updated_at`
 * (invalida cache do ToolRegistry, §10.2). NUNCA sobrescreve tools_config inteiro.
 */

// §7.7 — defaults quando não há registro (apenas as flags por-agente).
const DEFAULT_SETTINGS = {
  handoff_enabled: false,
  reopen_on_customer_reply: true,
  agent_can_close: false,
};

const SETTINGS_COLUMNS =
  'agent_id, company_id, handoff_enabled, ' +
  'reopen_on_customer_reply, agent_can_close, created_at, updated_at';

async function resolveAgent(
  request: Request,
  agentId: string,
): Promise<
  | {
      agent: { id: string; company_id: string; tools_config: Record<string, unknown> | null };
      session: AdminSession;
      response?: never;
    }
  | { response: NextResponse; agent?: never; session?: never }
> {
  const authResult = await requireAdminSession();
  if (authResult.response) {
    return { response: await authApiError(authResult.response, { request }) };
  }
  const session = authResult.session;

  const { data: agent, error } = await supabaseAdmin
    .from('agents')
    .select('id, company_id, tools_config')
    .eq('id', agentId)
    .single();

  if (error || !agent) {
    return { response: apiError('Agente não encontrado', { request, status: 404 }) };
  }

  if (session.role === 'company_admin' && agent.company_id !== session.companyId) {
    await auditCrossTenantAttempt({
      actorId: session.adminId,
      actorRole: session.role,
      actorCompanyId: session.companyId,
      resourceType: 'agents',
      resourceId: agentId,
      targetCompanyId: agent.company_id,
      action: 'attendance_settings',
      request,
    });
    return { response: apiError('Agente não encontrado', { request, status: 404 }) };
  }

  return {
    agent: agent as {
      id: string;
      company_id: string;
      tools_config: Record<string, unknown> | null;
    },
    session,
  };
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ agentId: string }> },
) {
  try {
    const { agentId } = await params;
    const resolved = await resolveAgent(request, agentId);
    if (resolved.response) return resolved.response;
    const { agent } = resolved;

    const { data: row, error } = await supabaseAdmin
      .from('agent_attendance_settings')
      .select(SETTINGS_COLUMNS)
      .eq('agent_id', agentId)
      .maybeSingle();

    if (error) {
      return apiError('Erro ao buscar configurações de atendimento', {
        cause: error,
        logMessage: '[ATTENDANCE SETTINGS] select failed',
        request,
        status: 500,
      });
    }

    // §9.3: GET retorna defaults mesmo SEM registro.
    const settings = row
      ? row
      : { agent_id: agentId, company_id: agent.company_id, ...DEFAULT_SETTINGS };

    return NextResponse.json({ settings });
  } catch (error: unknown) {
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[ATTENDANCE SETTINGS] GET error',
      request,
      status: 500,
    });
  }
}

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ agentId: string }> },
) {
  try {
    const { agentId } = await params;
    const resolved = await resolveAgent(request, agentId);
    if (resolved.response) return resolved.response;
    const { agent } = resolved;

    const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;

    // Carrega o registro atual (ou defaults) e aplica somente as chaves enviadas.
    const { data: existing } = await supabaseAdmin
      .from('agent_attendance_settings')
      .select(SETTINGS_COLUMNS)
      .eq('agent_id', agentId)
      .maybeSingle();

    const base: Record<string, unknown> = existing
      ? (existing as unknown as Record<string, unknown>)
      : { agent_id: agentId, company_id: agent.company_id, ...DEFAULT_SETTINGS };

    const merged: Record<string, unknown> = { ...base };
    const ALLOWED_KEYS = Object.keys(DEFAULT_SETTINGS);
    for (const key of ALLOWED_KEYS) {
      if (body[key] !== undefined) merged[key] = body[key];
    }

    const nowIso = new Date().toISOString();

    // 1. Upsert na FONTE DE VERDADE (apenas flags por-agente; auto_close_* é
    // company-level, gravado por /api/admin/company/attendance-settings).
    const { error: upsertError } = await supabaseAdmin.from('agent_attendance_settings').upsert(
      {
        agent_id: agentId,
        company_id: agent.company_id,
        handoff_enabled: merged.handoff_enabled,
        reopen_on_customer_reply: merged.reopen_on_customer_reply,
        agent_can_close: merged.agent_can_close,
        updated_at: nowIso,
      },
      { onConflict: 'agent_id' },
    );

    if (upsertError) {
      return apiError('Erro ao salvar configurações de atendimento', {
        cause: upsertError,
        logMessage: '[ATTENDANCE SETTINGS] upsert failed',
        request,
        status: 500,
      });
    }

    // 2. DEEP-MERGE em agents.tools_config: LÊ o existente e atualiza SÓ os
    // espelhos human_handoff.enabled / end_attendance.enabled, preservando
    // csv_analytics e chaves desconhecidas (§9.3). Bumpa updated_at.
    const nextToolsConfig = mergeAttendanceToolsConfig(agent.tools_config, {
      handoffEnabled: !!merged.handoff_enabled,
      agentCanClose: !!merged.agent_can_close,
    });

    const { error: agentUpdateError } = await supabaseAdmin
      .from('agents')
      .update({ tools_config: nextToolsConfig, updated_at: nowIso })
      .eq('id', agentId)
      .eq('company_id', agent.company_id);

    if (agentUpdateError) {
      log.error('[ATTENDANCE SETTINGS] tools_config mirror update failed', {
        errorCode: agentUpdateError.code,
        agentId,
      });
      return apiError('Erro ao espelhar configurações no agente', {
        cause: agentUpdateError,
        request,
        status: 500,
      });
    }

    const { data: saved } = await supabaseAdmin
      .from('agent_attendance_settings')
      .select(SETTINGS_COLUMNS)
      .eq('agent_id', agentId)
      .maybeSingle();

    return NextResponse.json({ settings: saved ?? merged });
  } catch (error: unknown) {
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[ATTENDANCE SETTINGS] PATCH error',
      request,
      status: 500,
    });
  }
}
