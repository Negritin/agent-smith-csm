import { NextRequest, NextResponse } from 'next/server';
import { apiError, authApiError } from '@/lib/api-error';
import { requireAdminSession } from '@/lib/auth-actions';
import { getSupabaseAdmin } from '@/lib/supabase-admin';
import {
  ExternalUrlValidationError,
  validateExternalUrl,
} from '@/lib/security/url-validator';
import { logSecurityAudit, summarizeAuditUrl } from '@/lib/security-audit';

const supabaseAdmin = getSupabaseAdmin();

async function validateWebhookUrlIfPresent(data: Record<string, unknown>) {
  if (typeof data.webhook_url !== 'string' || data.webhook_url.trim() === '') {
    return;
  }

  const webhookUrl = data.webhook_url.trim();
  data.webhook_url = webhookUrl;
  await validateExternalUrl(webhookUrl);
}

/**
 * GET /api/admin/companies
 * Returns list of companies with all fields.
 */
export async function GET(request: NextRequest) {
  try {
    const auth = await requireAdminSession();
    if (auth.response) return authApiError(auth.response, { request });
    const { session } = auth;

    const { searchParams } = new URL(request.url);
    const status = searchParams.get('status');

    let query = supabaseAdmin
      .from('companies')
      .select('*')
      .order('created_at', { ascending: false });

    if (session.role === 'company_admin') {
      query = query.eq('id', session.companyId);
    } else if (status && status !== 'all') {
      query = query.eq('status', status);
    }

    const { data: companies, error } = await query;

    if (error) {
      return apiError('Error fetching companies', {
        cause: error,
        logMessage: '[ADMIN COMPANIES] Error fetching companies',
        request,
        status: 500,
      });
    }

    // Buscar subscriptions ativas com dados do plano
    let subscriptionsQuery = supabaseAdmin
      .from('subscriptions')
      .select('company_id, status, current_period_end, plans(name, price_brl, display_credits)')
      .in('status', ['active', 'past_due']);

    if (session.role === 'company_admin') {
      subscriptionsQuery = subscriptionsQuery.eq('company_id', session.companyId);
    }

    const { data: subscriptions } = await subscriptionsQuery;

    // Buscar saldos de créditos
    let creditsQuery = supabaseAdmin.from('company_credits').select('company_id, balance_brl');

    if (session.role === 'company_admin') {
      creditsQuery = creditsQuery.eq('company_id', session.companyId);
    }

    const { data: credits } = await creditsQuery;

    // Criar mapa de subscription por company_id
    const subscriptionMap: Record<
      string,
      {
        plan_name: string;
        plan_price: number;
        display_credits: number;
        current_period_end: string | null;
        status: string;
      }
    > = {};
    for (const sub of subscriptions || []) {
      const plan = sub.plans as any;
      if (plan) {
        subscriptionMap[sub.company_id] = {
          plan_name: plan.name || '',
          plan_price: parseFloat(plan.price_brl || '0'),
          display_credits: plan.display_credits || 0,
          current_period_end: sub.current_period_end,
          status: sub.status,
        };
      }
    }

    // Criar mapa de créditos por company_id
    const creditsMap: Record<string, number> = {};
    for (const credit of credits || []) {
      creditsMap[credit.company_id] = parseFloat(credit.balance_brl || '0');
    }

    // Adicionar dados de subscription e créditos em cada empresa
    const companiesWithPlan = (companies || []).map((company) => {
      const sub = subscriptionMap[company.id];
      const balanceBrl = creditsMap[company.id] || 0;

      // Calcular créditos proporcionais
      let creditsRemaining = 0;
      if (sub && sub.plan_price > 0) {
        creditsRemaining = Math.floor((balanceBrl / sub.plan_price) * sub.display_credits);
      }

      return {
        ...company,
        subscription: sub || null,
        balance_brl: balanceBrl,
        credits_remaining: creditsRemaining,
      };
    });

    return NextResponse.json({ companies: companiesWithPlan });
  } catch (error: unknown) {
    return apiError('Internal server error', {
      cause: error,
      logMessage: '[ADMIN COMPANIES] Error',
      request,
      status: 500,
    });
  }
}

/**
 * POST /api/admin/companies
 * Creates a new company.
 */
export async function POST(request: NextRequest) {
  try {
    const auth = await requireAdminSession();
    if (auth.response) return authApiError(auth.response, { request });
    const { session } = auth;

    if (session.role !== 'master_admin') {
      return apiError('Não autorizado', { request, status: 403 });
    }

    const body = await request.json();
    await validateWebhookUrlIfPresent(body);

    const { data, error } = await supabaseAdmin.from('companies').insert([body]).select().single();

    if (error) {
      return apiError('Erro ao criar empresa', {
        cause: error,
        logMessage: '[ADMIN COMPANIES] Create error',
        request,
        status: 500,
      });
    }

    if (Object.prototype.hasOwnProperty.call(body, 'webhook_url')) {
      await logSecurityAudit({
        action: 'company_webhook_url_created',
        actorId: session.adminId,
        actorRole: session.role,
        companyId: data.id,
        targetCompanyId: data.id,
        resourceType: 'companies',
        resourceId: data.id,
        request,
        status: 'success',
        details: {
          webhookUrl: summarizeAuditUrl(body.webhook_url),
        },
      });
    }

    return NextResponse.json({ company: data }, { status: 201 });
  } catch (error: unknown) {
    if (error instanceof ExternalUrlValidationError) {
      return apiError('Webhook URL inválida', {
        cause: error,
        logMessage: '[ADMIN COMPANIES] Invalid webhook URL',
        request,
        status: 422,
      });
    }
    return apiError('Internal server error', {
      cause: error,
      logMessage: '[ADMIN COMPANIES] Error',
      request,
      status: 500,
    });
  }
}

/**
 * PUT /api/admin/companies
 * Updates an existing company.
 * Only Master Admin can edit companies.
 */
export async function PUT(request: NextRequest) {
  try {
    const auth = await requireAdminSession();
    if (auth.response) return authApiError(auth.response, { request });
    const { session } = auth;

    if (session.role !== 'master_admin') {
      return apiError('Não autorizado', { request, status: 403 });
    }

    const body = await request.json();
    const { id, ...updateData } = body;

    if (!id) {
      return apiError('Company ID is required', { request, status: 400 });
    }

    await validateWebhookUrlIfPresent(updateData);
    const webhookUrlWasProvided = Object.prototype.hasOwnProperty.call(updateData, 'webhook_url');
    let previousWebhookUrl: string | null = null;

    if (webhookUrlWasProvided) {
      const { data: previousCompany } = await supabaseAdmin
        .from('companies')
        .select('webhook_url')
        .eq('id', id)
        .maybeSingle();

      previousWebhookUrl =
        typeof previousCompany?.webhook_url === 'string' ? previousCompany.webhook_url : null;
    }

    // VALIDATION: If max_users is being updated, check current admin count
    if (updateData.max_users !== undefined) {
      const newMaxUsers = parseInt(updateData.max_users);

      if (isNaN(newMaxUsers) || newMaxUsers < 1) {
        return apiError('Máximo de administradores deve ser pelo menos 1', {
          request,
          status: 400,
        });
      }

      // Count current active admins in the company
      const { count: adminCount } = await supabaseAdmin
        .from('users_v2')
        .select('*', { count: 'exact', head: true })
        .eq('company_id', id)
        .in('role', ['admin_company', 'owner', 'admin'])
        .neq('status', 'suspended');

      if ((adminCount || 0) > newMaxUsers) {
        return apiError(
          `Não é possível reduzir para ${newMaxUsers} administradores. Existem ${adminCount} administradores ativos na empresa.`,
          { request, status: 400 },
        );
      }
    }

    const { data, error } = await supabaseAdmin
      .from('companies')
      .update(updateData)
      .eq('id', id)
      .select()
      .single();

    if (error) {
      return apiError('Erro ao atualizar empresa', {
        cause: error,
        logMessage: '[ADMIN COMPANIES] Update error',
        request,
        status: 500,
      });
    }

    if (webhookUrlWasProvided && previousWebhookUrl !== updateData.webhook_url) {
      await logSecurityAudit({
        action: 'company_webhook_url_updated',
        actorId: session.adminId,
        actorRole: session.role,
        companyId: id,
        targetCompanyId: id,
        resourceType: 'companies',
        resourceId: id,
        request,
        status: 'success',
        details: {
          previousWebhookUrl: summarizeAuditUrl(previousWebhookUrl),
          newWebhookUrl: summarizeAuditUrl(updateData.webhook_url),
        },
      });
    }

    return NextResponse.json({ company: data });
  } catch (error: unknown) {
    if (error instanceof ExternalUrlValidationError) {
      return apiError('Webhook URL inválida', {
        cause: error,
        logMessage: '[ADMIN COMPANIES] Invalid webhook URL',
        request,
        status: 422,
      });
    }
    return apiError('Internal server error', {
      cause: error,
      logMessage: '[ADMIN COMPANIES] Error',
      request,
      status: 500,
    });
  }
}
