import { NextRequest, NextResponse } from 'next/server';
import { apiError, authApiError } from '@/lib/api-error';
import { requireAdminSession } from '@/lib/auth-actions';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

export const dynamic = 'force-dynamic';

const supabaseAdmin = getSupabaseAdmin();

/**
 * GET/PUT /api/admin/company/attendance-settings (§13/§16).
 *
 * Encerramento automático por inatividade (auto_close_*) é config da EMPRESA
 * (NÃO por agente nem por admin): vive em `company_attendance_settings` com PK =
 * company_id. O worker de inatividade (§16) e o InactivityTimerService leem por
 * company_id. master_admin precisa enviar `company_id` na query; company_admin
 * usa a própria empresa. GET retorna defaults quando ausente; PUT faz upsert por
 * company_id e valida after_minutes>=5 + mensagem obrigatória quando habilitada.
 */

const SETTINGS_COLUMNS =
  'company_id, auto_close_enabled, auto_close_after_minutes, auto_close_scope, ' +
  'auto_close_message_enabled, auto_close_message, created_at, updated_at';

// Defaults idênticos aos da migration (20260628_01) e da rota do agente — para
// que GET sem registro entregue exatamente o estado padrão do banco.
const DEFAULT_SETTINGS = {
  auto_close_enabled: false,
  auto_close_after_minutes: 240,
  auto_close_scope: 'all_attendance' as const,
  auto_close_message_enabled: true,
  auto_close_message:
    'Encerramos este atendimento por falta de resposta. Se precisar, é só chamar novamente.',
};

const EDITABLE_KEYS = [
  'auto_close_enabled',
  'auto_close_after_minutes',
  'auto_close_scope',
  'auto_close_message_enabled',
  'auto_close_message',
] as const;

async function resolveCompanyId(
  request: Request,
): Promise<{ companyId: string; response?: never } | { response: NextResponse }> {
  const result = await requireAdminSession();
  if (result.response) {
    return { response: await authApiError(result.response, { request }) };
  }
  const session = result.session;

  if (session.role === 'company_admin') {
    if (!session.companyId) {
      return { response: apiError('Não autorizado', { request, status: 403 }) };
    }
    return { companyId: session.companyId };
  }

  const url = new URL(request.url);
  const queryCompanyId = url.searchParams.get('company_id') || url.searchParams.get('companyId');
  if (!queryCompanyId) {
    return {
      response: apiError('company_id é obrigatório para master_admin', { request, status: 400 }),
    };
  }
  return { companyId: queryCompanyId };
}

export async function GET(request: NextRequest) {
  try {
    const resolved = await resolveCompanyId(request);
    if (resolved.response) return resolved.response;

    const { data: row, error } = await supabaseAdmin
      .from('company_attendance_settings')
      .select(SETTINGS_COLUMNS)
      .eq('company_id', resolved.companyId)
      .maybeSingle();

    if (error) {
      return apiError('Erro ao buscar configurações de encerramento', {
        cause: error,
        logMessage: '[COMPANY ATTENDANCE] select failed',
        request,
        status: 500,
      });
    }

    // Sem registro => defaults (auto-close OFF), igual ao default do banco.
    const settings = row ? row : { company_id: resolved.companyId, ...DEFAULT_SETTINGS };

    return NextResponse.json({ settings });
  } catch (error: unknown) {
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[COMPANY ATTENDANCE] GET error',
      request,
      status: 500,
    });
  }
}

export async function PUT(request: NextRequest) {
  try {
    const resolved = await resolveCompanyId(request);
    if (resolved.response) return resolved.response;
    const { companyId } = resolved;

    const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;

    // Carrega o registro atual (ou defaults) e aplica somente as chaves enviadas.
    const { data: existing } = await supabaseAdmin
      .from('company_attendance_settings')
      .select(SETTINGS_COLUMNS)
      .eq('company_id', companyId)
      .maybeSingle();

    const base: Record<string, unknown> = existing
      ? (existing as unknown as Record<string, unknown>)
      : { company_id: companyId, ...DEFAULT_SETTINGS };

    const merged: Record<string, unknown> = { ...base };
    for (const key of EDITABLE_KEYS) {
      if (body[key] !== undefined) merged[key] = body[key];
    }

    // Validação leve do domínio (espelha CHECKs da migration 20260628_01).
    if (merged.auto_close_scope !== 'all_attendance' && merged.auto_close_scope !== 'human_only') {
      return apiError("auto_close_scope inválido (use 'all_attendance' ou 'human_only')", {
        request,
        status: 400,
      });
    }
    // after_minutes >= 5 (CHECK auto_close_after_minutes >= 5).
    if (typeof merged.auto_close_after_minutes !== 'number' || merged.auto_close_after_minutes < 5) {
      return apiError('auto_close_after_minutes deve ser um número >= 5', {
        request,
        status: 400,
      });
    }
    // Mensagem final obrigatória quando habilitada (company_attendance_message_check):
    // sem isto, uma chamada direta gravaria message_enabled=true com texto vazio e o
    // worker de inatividade (§16) tentaria enviar uma "mensagem final" vazia.
    if (
      merged.auto_close_message_enabled === true &&
      (typeof merged.auto_close_message !== 'string' ||
        merged.auto_close_message.trim().length === 0)
    ) {
      return apiError('A mensagem final é obrigatória quando o envio está habilitado', {
        request,
        status: 400,
      });
    }

    const nowIso = new Date().toISOString();

    // Upsert por company_id (PK). Mantém no máximo 1 linha por empresa.
    const { data: saved, error: upsertError } = await supabaseAdmin
      .from('company_attendance_settings')
      .upsert(
        {
          company_id: companyId,
          auto_close_enabled: merged.auto_close_enabled,
          auto_close_after_minutes: merged.auto_close_after_minutes,
          auto_close_scope: merged.auto_close_scope,
          auto_close_message_enabled: merged.auto_close_message_enabled,
          auto_close_message: merged.auto_close_message,
          updated_at: nowIso,
        },
        { onConflict: 'company_id' },
      )
      .select(SETTINGS_COLUMNS)
      .single();

    if (upsertError) {
      return apiError('Erro ao salvar configurações de encerramento', {
        cause: upsertError,
        logMessage: '[COMPANY ATTENDANCE] upsert failed',
        request,
        status: 500,
      });
    }

    return NextResponse.json({ settings: saved });
  } catch (error: unknown) {
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[COMPANY ATTENDANCE] PUT error',
      request,
      status: 500,
    });
  }
}
