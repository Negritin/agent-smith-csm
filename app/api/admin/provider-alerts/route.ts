import { NextRequest, NextResponse } from 'next/server';
import { cookies } from 'next/headers';
import { getIronSession } from 'iron-session';
import { createClient } from '@supabase/supabase-js';
import { adminSessionOptions, AdminSessionData } from '@/lib/iron-session';
import { log, errorLogFields } from '@/lib/logger';

export const dynamic = 'force-dynamic';

// Service Role Client (bypasses RLS — platform_provider_alerts is locked down).
const supabaseAdmin = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY!,
  { auth: { persistSession: false } },
);

/**
 * GET /api/admin/provider-alerts
 *
 * MASTER-ONLY. Returns active LLM-provider out-of-balance alerts for the red
 * banner in the master admin. The signal is PLATFORM-internal (the LLM keys are
 * platform-wide, not BYO per company), so it must NEVER reach a tenant:
 * non-master sessions (company admins / members / anon) always get an empty
 * list — never the data, never a revealing 403.
 *
 * "Master" is decided at the DB level: the admin session id must resolve to a
 * row in `admin_users` (same gate as /api/admin/me).
 */
export async function GET(_request: NextRequest) {
  try {
    const cookieStore = await cookies();
    const adminSession = await getIronSession<AdminSessionData>(cookieStore, adminSessionOptions);

    if (!adminSession.adminId) {
      return NextResponse.json({ alerts: [] });
    }

    // Master admin == a row in admin_users for this adminId. Company admins live
    // in users_v2 and resolve to nothing here -> empty list (tenant never sees it).
    const { data: masterAdmin } = await supabaseAdmin
      .from('admin_users')
      .select('id')
      .eq('id', adminSession.adminId)
      .single();

    if (!masterAdmin) {
      return NextResponse.json({ alerts: [] });
    }

    const { data: alerts, error } = await supabaseAdmin
      .from('platform_provider_alerts')
      .select('provider, kind, message, detected_at')
      .is('resolved_at', null)
      .order('detected_at', { ascending: true });

    if (error) {
      log.error('[PROVIDER ALERTS] query failed', errorLogFields(error));
      return NextResponse.json({ alerts: [] });
    }

    return NextResponse.json({ alerts: alerts ?? [] });
  } catch (error: unknown) {
    log.error('[PROVIDER ALERTS] critical error', errorLogFields(error));
    return NextResponse.json({ alerts: [] });
  }
}
