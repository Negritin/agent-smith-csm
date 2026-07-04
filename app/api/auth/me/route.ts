import { NextRequest, NextResponse } from 'next/server';
import { cookies } from 'next/headers';
import { getIronSession } from 'iron-session';
import { createClient } from '@supabase/supabase-js';
import {
  sessionOptions,
  adminSessionOptions,
  SessionData,
  AdminSessionData,
} from '@/lib/iron-session';

export const dynamic = 'force-dynamic';

// Service Role Client
const supabaseAdmin = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY!,
  { auth: { persistSession: false } },
);

/**
 * GET /api/auth/me
 *
 * Returns the current user session info
 * Used by hooks to determine user role
 * Also checks if user needs to re-accept terms of use
 */
export async function GET(request: NextRequest) {
  try {
    const cookieStore = await cookies();

    // Try user session first
    const userSession = await getIronSession<SessionData>(cookieStore, sessionOptions);

    let userId: string | null = null;
    let isMasterAdmin = false;

    if (userSession.userId) {
      userId = userSession.userId;
    } else {
      // If no user session, try admin session
      const adminSession = await getIronSession<AdminSessionData>(cookieStore, adminSessionOptions);
      if (adminSession.adminId) {
        userId = adminSession.adminId;

        // Check if this is a master admin (from admin_users table)
        const { data: masterCheck } = await supabaseAdmin
          .from('admin_users')
          .select('id')
          .eq('id', adminSession.adminId)
          .maybeSingle();

        isMasterAdmin = !!masterCheck;
      }
    }

    if (!userId) {
      return NextResponse.json({ user: null }, { status: 200 });
    }

    // Get user details from database
    const { data: user, error } = await supabaseAdmin
      .from('users_v2')
      .select('id, email, first_name, last_name, company_id, role, status, is_owner, avatar_url, accepted_terms_version')
      .eq('id', userId)
      .single();

    if (error || !user) {
      return NextResponse.json({ user: null }, { status: 200 });
    }

    // Check terms acceptance (skip for master admins)
    let termsOutdated = false;
    let activeTerms = null;

    if (!isMasterAdmin) {
      const { data: activeDoc } = await supabaseAdmin
        .from('legal_documents')
        .select('id, title, content, version')
        .eq('type', 'terms_of_use')
        .eq('is_active', true)
        .maybeSingle();

      if (activeDoc && activeDoc.id !== user.accepted_terms_version) {
        termsOutdated = true;
        activeTerms = activeDoc;
      }
    }

    // Return user info
    return NextResponse.json({
      user: {
        id: user.id,
        email: user.email,
        first_name: user.first_name,
        last_name: user.last_name,
        company_id: user.company_id,
        role: user.role,
        status: user.status,
        is_owner: user.is_owner || false,
        avatar_url: user.avatar_url || null,
      },
      termsOutdated,
      activeTerms,
    });
  } catch (error) {
    console.error('[AUTH ME] Error:', error);
    return NextResponse.json({ user: null }, { status: 200 });
  }
}
