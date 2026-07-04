import { NextRequest, NextResponse } from 'next/server';
import { cookies } from 'next/headers';
import { getIronSession } from 'iron-session';
import { createClient } from '@supabase/supabase-js';
import { adminSessionOptions, AdminSessionData } from '@/lib/iron-session';

export const dynamic = 'force-dynamic';

// Service Role Client
const supabaseAdmin = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY!,
  { auth: { persistSession: false } },
);

/**
 * GET /api/admin/team
 * Returns team members for the authenticated company admin
 */
export async function GET(request: NextRequest) {
  try {
    const cookieStore = await cookies();
    const session = await getIronSession<AdminSessionData>(cookieStore, adminSessionOptions);

    if (!session.adminId) {
      return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
    }

    const companyId = session.companyId;
    if (!companyId) {
      return NextResponse.json({ error: 'Company not found in session' }, { status: 400 });
    }

    // Get active and suspended users
    const { data: teamData, error: teamError } = await supabaseAdmin
      .from('users_v2')
      .select('id, email, first_name, last_name, role, status, is_owner, created_at')
      .eq('company_id', companyId)
      .in('status', ['active', 'suspended'])
      .order('is_owner', { ascending: false })
      .order('created_at', { ascending: false });

    if (teamError) {
      console.error('[TEAM API] Error:', teamError);
      return NextResponse.json({ error: 'Error loading team' }, { status: 500 });
    }

    // Get pending users
    const { data: pendingData, error: pendingError } = await supabaseAdmin
      .from('users_v2')
      .select('id, email, first_name, last_name, role, status, is_owner, created_at')
      .eq('company_id', companyId)
      .eq('status', 'pending')
      .order('created_at', { ascending: false });

    if (pendingError) {
      console.error('[TEAM API] Error loading pending:', pendingError);
    }

    return NextResponse.json({
      users: teamData || [],
      pendingUsers: pendingData || [],
    });
  } catch (error: any) {
    console.error('[TEAM API] Error:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
