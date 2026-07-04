import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import type { WidgetBootstrapPayload } from '@/lib/security/widget-bootstrap';
import {
  getPublicWidgetAgent,
  getWidgetBootstrapPayload,
  getWidgetHmacSecret,
  isOriginValueAllowed,
} from '@/lib/security/widget-origin';

export const dynamic = 'force-dynamic';

// Service Role Client (bypassa RLS)
const supabaseAdmin = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY!,
  { auth: { persistSession: false } },
);

/**
 * POST /api/leads/identify
 *
 * Identifica ou cria um lead baseado no e-mail.
 *
 * SEGURANÇA (F03): exige o MESMO proof de bootstrap HMAC de /api/widget/messages
 * (verifyWidgetBootstrapToken + nonce-cookie + allowlist de origem). O companyId
 * é derivado do AGENTE verificado (get_widget_agent_public) — NUNCA do body — e a
 * resposta é não-diferencial (sempre { leadId, isNew: false } sem ecoar name nem
 * existência) para não vazar PII/existência de lead entre tenants.
 */
export async function POST(req: NextRequest) {
  try {
    const secret = getWidgetHmacSecret();
    if (!secret) {
      return NextResponse.json({ error: 'Widget security is not configured' }, { status: 403 });
    }

    const body = await req.json().catch(() => ({}));
    const email = typeof body.email === 'string' ? body.email : '';
    const name = typeof body.name === 'string' ? body.name : '';
    const agentId = typeof body.agentId === 'string' ? body.agentId : body.agent_id;
    const bootstrapToken =
      typeof body.bootstrap_token === 'string' ? body.bootstrap_token : body.bootstrapToken;

    if (!email) {
      return NextResponse.json({ error: 'Email é obrigatório' }, { status: 400 });
    }

    // Validação básica de e-mail
    const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    if (!emailRegex.test(email)) {
      return NextResponse.json({ error: 'E-mail inválido' }, { status: 400 });
    }

    if (typeof agentId !== 'string') {
      return NextResponse.json({ error: 'agentId é obrigatório' }, { status: 400 });
    }

    if (typeof bootstrapToken !== 'string') {
      return NextResponse.json({ error: 'Widget bootstrap is required' }, { status: 401 });
    }

    // 1. Verificar o proof de bootstrap (assinatura + nonce-cookie)
    let bootstrapPayload: WidgetBootstrapPayload;
    try {
      bootstrapPayload = getWidgetBootstrapPayload(req, bootstrapToken, secret);
    } catch {
      return NextResponse.json({ error: 'Invalid widget bootstrap' }, { status: 401 });
    }

    // 2. Carregar o agente público e derivar o tenant DELE (nunca do body)
    const agent = await getPublicWidgetAgent(agentId, '[LEADS API]');
    if (!agent) {
      return NextResponse.json({ error: 'Widget not found' }, { status: 404 });
    }

    if (
      bootstrapPayload.agentId !== agent.id ||
      bootstrapPayload.companyId !== agent.company_id
    ) {
      return NextResponse.json({ error: 'Invalid widget bootstrap' }, { status: 401 });
    }

    // 3. Validar a origem do bootstrap contra a allowlist do widget
    if (!isOriginValueAllowed(bootstrapPayload.origin, agent.widget_config)) {
      return NextResponse.json({ error: 'Origin not allowed' }, { status: 403 });
    }

    const companyId = agent.company_id;
    const normalizedEmail = email.toLowerCase().trim();

    // 4. Tenta encontrar lead existente (no tenant DERIVADO do agente)
    const { data: existing } = await supabaseAdmin
      .from('leads')
      .select('id')
      .eq('company_id', companyId)
      .eq('email', normalizedEmail)
      .single();

    if (existing) {
      // Apenas bump de last_seen_at — NÃO sobrescrever name por chamada anônima.
      await supabaseAdmin
        .from('leads')
        .update({ last_seen_at: new Date().toISOString() })
        .eq('id', existing.id);

      // Resposta não-diferencial: idêntica à de um e-mail novo (sempre isNew
      // false, sem ecoar name nem a existência do lead).
      return NextResponse.json({ leadId: existing.id, isNew: false });
    }

    // 5. Cria novo lead
    const { data: newLead, error } = await supabaseAdmin
      .from('leads')
      .insert({
        company_id: companyId,
        email: normalizedEmail,
        name: name?.trim() || null,
        last_seen_at: new Date().toISOString(),
      })
      .select('id')
      .single();

    if (error) {
      console.error('[LEADS API] Insert error:', error);
      throw error;
    }

    return NextResponse.json({ leadId: newLead.id, isNew: false });
  } catch (error) {
    console.error('[LEADS API] Error identifying lead:', error);
    return NextResponse.json({ error: 'Falha ao processar identificação' }, { status: 500 });
  }
}
