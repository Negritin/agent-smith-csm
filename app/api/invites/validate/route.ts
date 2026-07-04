import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';

// Service Role Client
const supabaseAdmin = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY!,
  { auth: { persistSession: false } },
);

/**
 * POST /api/invites/validate
 *
 * Valida um token de convite e retorna informações da empresa
 */
export async function POST(request: NextRequest) {
  try {
    const { token } = await request.json();

    if (!token) {
      return NextResponse.json({ error: 'Token is required' }, { status: 400 });
    }

    // Buscar invite pelo token
    const { data: invite, error: inviteError } = await supabaseAdmin
      .from('invites')
      .select(
        `
        id,
        company_id,
        role,
        email,
        name,
        max_uses,
        current_uses,
        expires_at,
        companies:company_id (
          company_name
        )
      `,
      )
      .eq('token', token)
      .single();

    if (inviteError || !invite) {
      return NextResponse.json(
        { error: 'Invalid or expired invite token', valid: false },
        { status: 404 },
      );
    }

    // Verificar se expirou
    const now = new Date();
    const expiresAt = new Date(invite.expires_at);

    if (expiresAt < now) {
      return NextResponse.json(
        { error: 'Invite token has expired', valid: false },
        { status: 410 }, // 410 Gone
      );
    }

    // Verificar se excedeu número de usos
    if (invite.current_uses >= invite.max_uses) {
      return NextResponse.json(
        { error: 'Invite token has reached maximum uses', valid: false },
        { status: 410 },
      );
    }

    // Token válido - retornar informações
    const companyName = Array.isArray(invite.companies)
      ? (invite.companies as any)[0]?.company_name
      : (invite.companies as any)?.company_name;

    return NextResponse.json({
      valid: true,
      companyId: invite.company_id,
      companyName: companyName || 'Unknown Company',
      inviteId: invite.id,
      role: invite.role || 'member',
      email: invite.email || null,
      name: invite.name || null,
    });
  } catch (error: any) {
    console.error('[INVITE VALIDATE] Error:', error);
    return NextResponse.json({ error: 'Internal server error', valid: false }, { status: 500 });
  }
}
