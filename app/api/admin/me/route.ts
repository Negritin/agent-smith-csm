import { NextRequest, NextResponse } from 'next/server';
import { cookies } from 'next/headers';
import { getIronSession } from 'iron-session';
import { createClient } from '@supabase/supabase-js';
import { apiError } from '@/lib/api-error';
import {
  sessionOptions,
  adminSessionOptions,
  SessionData,
  AdminSessionData,
} from '@/lib/iron-session';
import { log, errorLogFields } from '@/lib/logger';

export const dynamic = 'force-dynamic';

// Service Role Client
const supabaseAdmin = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY!,
  { auth: { persistSession: false } },
);

/**
 * GET /api/admin/me
 *
 * SECURE ADMIN ENDPOINT - Session Leakage Protection
 *
 * This endpoint returns admin data ONLY for:
 * - Master Admin (from admin_users table via smith_admin_session)
 * - Company Admin (from users_v2 with role IN ['admin_company', 'owner', 'admin'])
 *
 * CRITICAL: If user is a 'member', this endpoint returns 403.
 * The role filter is applied at DATABASE LEVEL, not in JavaScript.
 */
export async function GET(request: NextRequest) {
  try {
    const cookieStore = await cookies();

    // ========================================
    // PRIORITY 1: Admin Session (Master or Company Admin)
    // ========================================
    const adminSession = await getIronSession<AdminSessionData>(cookieStore, adminSessionOptions);

    if (adminSession.adminId) {
      const adminId = adminSession.adminId;
      log.debug('[ADMIN ME] Admin session detected', { adminIdPresent: true });

      // Try admin_users table first (Master Admin)
      const { data: masterAdmin, error: masterError } = await supabaseAdmin
        .from('admin_users')
        .select('id, email, name')
        .eq('id', adminId)
        .single();

      if (!masterError && masterAdmin) {
        log.debug('[ADMIN ME] Master Admin found', { adminIdPresent: true });
        return NextResponse.json({
          user: {
            id: masterAdmin.id,
            email: masterAdmin.email,
            first_name: masterAdmin.name?.split(' ')[0] || 'Admin',
            last_name: masterAdmin.name?.split(' ').slice(1).join(' ') || 'Master',
            company_id: null,
            role: 'master',
            status: 'active',
            is_owner: true,
            cpf: '',
            birth_date: '',
            avatar_url: '',
          },
          sessionType: 'master_admin',
        });
      }

      // Try users_v2 with ROLE FILTER at database level
      const { data: companyAdmin, error: companyError } = await supabaseAdmin
        .from('users_v2')
        .select(
          'id, email, first_name, last_name, company_id, role, status, is_owner, cpf, birth_date, avatar_url, companies(company_name)',
        )
        .eq('id', adminId)
        .in('role', ['admin_company', 'owner', 'admin'])
        .single();

      if (!companyError && companyAdmin) {
        log.debug('[ADMIN ME] Company Admin found via admin session', { adminIdPresent: true });
        return NextResponse.json({
          user: {
            id: companyAdmin.id,
            email: companyAdmin.email,
            first_name: companyAdmin.first_name,
            last_name: companyAdmin.last_name,
            company_id: companyAdmin.company_id,
            role: companyAdmin.role,
            status: companyAdmin.status,
            is_owner: companyAdmin.is_owner || false,
            cpf: companyAdmin.cpf,
            birth_date: companyAdmin.birth_date,
            avatar_url: companyAdmin.avatar_url,
          },
          company: {
            company_name: (companyAdmin.companies as any)?.company_name || 'Empresa',
          },
          sessionType: 'company_admin',
        });
      }

      log.warn('[ADMIN ME] Admin session exists but no valid admin was found', {
        adminIdPresent: true,
      });
    }

    // ========================================
    // PRIORITY 2: User Session (Company Admin only)
    // ========================================
    const userSession = await getIronSession<SessionData>(cookieStore, sessionOptions);

    if (userSession.userId) {
      const userId = userSession.userId;
      log.debug('[ADMIN ME] User session detected', { userIdPresent: true });

      // CRITICAL: Filter by ROLE at DATABASE level
      const { data: user, error } = await supabaseAdmin
        .from('users_v2')
        .select(
          'id, email, first_name, last_name, company_id, role, status, is_owner, cpf, birth_date, avatar_url, companies(company_name)',
        )
        .eq('id', userId)
        .in('role', ['admin_company', 'owner', 'admin'])
        .single();

      if (error || !user) {
        log.warn('[ADMIN ME] Blocked non-admin user session', { userIdPresent: true });
        return apiError('Acesso negado. Você não tem permissão de administrador.', {
          request,
          status: 403,
        });
      }

      log.debug('[ADMIN ME] Company Admin validated', { userIdPresent: true });
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
          cpf: user.cpf,
          birth_date: user.birth_date,
          avatar_url: user.avatar_url,
        },
        company: {
          company_name: (user.companies as any)?.company_name || 'Empresa',
        },
        sessionType: 'company_admin',
      });
    }

    // ========================================
    // NO VALID SESSION
    // ========================================
    log.debug('[ADMIN ME] No valid session found');
    return apiError('Sessão não encontrada', { request, status: 401 });
  } catch (error: unknown) {
    log.error('[ADMIN ME] Critical error', errorLogFields(error));
    return apiError('Erro interno do servidor', {
      cause: error,
      logMessage: '[ADMIN ME] Critical error',
      request,
      status: 500,
    });
  }
}
