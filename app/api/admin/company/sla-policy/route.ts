import { NextRequest, NextResponse } from 'next/server';
import { apiError, authApiError } from '@/lib/api-error';
import { requireAdminSession } from '@/lib/auth-actions';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

export const dynamic = 'force-dynamic';

const supabaseAdmin = getSupabaseAdmin();

/**
 * GET/PUT /api/admin/company/sla-policy (§9.2, §14).
 *
 * Política de SLA por EMPRESA (não por admin). Mantém no máximo UMA política
 * ativa por company (`uq_sla_policies_one_active_per_company`): o PUT desativa as
 * ativas anteriores e cria a nova ativa (upsert por company_id ativo). master_admin
 * exige `company_id` na query; company_admin usa a própria empresa.
 */

const POLICY_COLUMNS =
  'id, company_id, name, is_active, timezone, business_hours_enabled, working_days, ' +
  'working_start, working_end, normal_first_response_minutes, normal_resolution_minutes, ' +
  'high_first_response_minutes, high_resolution_minutes, critical_first_response_minutes, ' +
  'critical_resolution_minutes, default_sla_level, created_at, updated_at';

/** Campos de prazo (devem ser inteiros > 0 — §7.4). */
const MINUTE_KEYS = [
  'normal_first_response_minutes',
  'normal_resolution_minutes',
  'high_first_response_minutes',
  'high_resolution_minutes',
  'critical_first_response_minutes',
  'critical_resolution_minutes',
] as const;

const EDITABLE_KEYS = [
  'name',
  'timezone',
  'business_hours_enabled',
  'working_days',
  'working_start',
  'working_end',
  'normal_first_response_minutes',
  'normal_resolution_minutes',
  'high_first_response_minutes',
  'high_resolution_minutes',
  'critical_first_response_minutes',
  'critical_resolution_minutes',
  'default_sla_level',
];

async function resolveCompanyId(
  request: Request,
): Promise<
  { companyId: string; creatorUserId: string | null; response?: never } | { response: NextResponse }
> {
  const result = await requireAdminSession();
  if (result.response) {
    return { response: await authApiError(result.response, { request }) };
  }
  const session = result.session;

  if (session.role === 'company_admin') {
    if (!session.companyId) {
      return { response: apiError('Não autorizado', { request, status: 403 }) };
    }
    // company_admin.adminId É uma linha de users_v2 → seguro como created_by.
    return { companyId: session.companyId, creatorUserId: session.adminId };
  }

  const url = new URL(request.url);
  const queryCompanyId = url.searchParams.get('company_id') || url.searchParams.get('companyId');
  if (!queryCompanyId) {
    return {
      response: apiError('company_id é obrigatório para master_admin', { request, status: 400 }),
    };
  }
  // master_admin.adminId vem de admin_users (SEM relação com users_v2). As colunas
  // sla_policies.created_by/updated_by fazem FK para users_v2(id), então passar o id
  // do master_admin causaria foreign_key_violation (23503). null evita o 500.
  return { companyId: queryCompanyId, creatorUserId: null };
}

export async function GET(request: NextRequest) {
  try {
    const resolved = await resolveCompanyId(request);
    if (resolved.response) return resolved.response;

    const { data: policy, error } = await supabaseAdmin
      .from('sla_policies')
      .select(POLICY_COLUMNS)
      .eq('company_id', resolved.companyId)
      .eq('is_active', true)
      .maybeSingle();

    if (error) {
      return apiError('Erro ao buscar política de SLA', {
        cause: error,
        logMessage: '[SLA POLICY] select failed',
        request,
        status: 500,
      });
    }

    // Sem política ativa => null (handoff funciona sem SLA, §22 item 5).
    return NextResponse.json({ policy: policy ?? null });
  } catch (error: unknown) {
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[SLA POLICY] GET error',
      request,
      status: 500,
    });
  }
}

export async function PUT(request: NextRequest) {
  try {
    const resolved = await resolveCompanyId(request);
    if (resolved.response) return resolved.response;
    const { companyId, creatorUserId } = resolved;

    const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;

    const fields: Record<string, unknown> = {};
    for (const key of EDITABLE_KEYS) {
      if (body[key] !== undefined) fields[key] = body[key];
    }

    if (
      fields.default_sla_level !== undefined &&
      !['normal', 'high', 'critical'].includes(fields.default_sla_level as string)
    ) {
      return apiError('default_sla_level inválido (normal|high|critical)', {
        request,
        status: 400,
      });
    }

    // Prazos positivos (§7.4 sla_policies_minutes_positive_check): cada *_minutes
    // enviado deve ser inteiro >= 1. Sem isto, um campo vazio (cliente envia 0)
    // viola o CHECK do banco e a rota retornaria um 500 opaco em vez de um 400
    // acionável.
    for (const key of MINUTE_KEYS) {
      if (fields[key] === undefined) continue;
      const value = fields[key];
      if (typeof value !== 'number' || !Number.isInteger(value) || value < 1) {
        return apiError(`${key} deve ser um número inteiro >= 1`, {
          request,
          status: 400,
        });
      }
    }

    // Horário útil (§7.4 sla_policies_business_hours_check): quando habilitado,
    // working_start < working_end e ao menos um dia útil. Evita 500 opaco vindo
    // do CHECK do banco quando a UI permite start>=end ou lista de dias vazia.
    if (fields.business_hours_enabled === true) {
      const start = typeof fields.working_start === 'string' ? fields.working_start : undefined;
      const end = typeof fields.working_end === 'string' ? fields.working_end : undefined;
      if (start !== undefined && end !== undefined && start >= end) {
        return apiError('O horário de início deve ser anterior ao de fim', {
          request,
          status: 400,
        });
      }
      if (Array.isArray(fields.working_days) && fields.working_days.length === 0) {
        return apiError('Selecione ao menos um dia útil quando o horário útil está habilitado', {
          request,
          status: 400,
        });
      }
    }

    const nowIso = new Date().toISOString();

    // Política ativa existente?
    const { data: existing, error: existingError } = await supabaseAdmin
      .from('sla_policies')
      .select('id')
      .eq('company_id', companyId)
      .eq('is_active', true)
      .maybeSingle();

    if (existingError) {
      return apiError('Erro ao buscar política de SLA', {
        cause: existingError,
        request,
        status: 500,
      });
    }

    if (existing) {
      // Atualiza a política ativa existente (mantém unicidade — não cria 2ª ativa).
      const { data: updated, error: updateError } = await supabaseAdmin
        .from('sla_policies')
        .update({ ...fields, updated_by: creatorUserId, updated_at: nowIso })
        .eq('id', existing.id)
        .eq('company_id', companyId)
        .select(POLICY_COLUMNS)
        .single();

      if (updateError) {
        return apiError('Erro ao salvar política de SLA', {
          cause: updateError,
          logMessage: '[SLA POLICY] update failed',
          request,
          status: 500,
        });
      }
      return NextResponse.json({ policy: updated });
    }

    // Sem política ativa: cria uma nova ativa.
    const { data: created, error: insertError } = await supabaseAdmin
      .from('sla_policies')
      .insert({
        company_id: companyId,
        is_active: true,
        created_by: creatorUserId,
        updated_by: creatorUserId,
        ...fields,
      })
      .select(POLICY_COLUMNS)
      .single();

    if (insertError) {
      return apiError('Erro ao criar política de SLA', {
        cause: insertError,
        logMessage: '[SLA POLICY] insert failed',
        request,
        status: 500,
      });
    }

    return NextResponse.json({ policy: created });
  } catch (error: unknown) {
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[SLA POLICY] PUT error',
      request,
      status: 500,
    });
  }
}
