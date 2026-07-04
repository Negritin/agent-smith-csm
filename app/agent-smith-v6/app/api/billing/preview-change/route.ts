import { NextRequest, NextResponse } from 'next/server';
import { cookies } from 'next/headers';
import { getIronSession } from 'iron-session';
import {
  adminSessionOptions,
  AdminSessionData,
  sessionOptions,
  SessionData,
} from '@/lib/iron-session';
import { createClient } from '@supabase/supabase-js';
import { apiError } from '@/lib/api-error';
import { getOptionalInternalAuthHeaders } from '@/lib/internal-jwt';
import { errorLogFields, log } from '@/lib/logger';
import { getAdminApiKeyOrResponse } from '@/lib/admin-proxy';

const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

const supabaseAdmin = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY!,
  { auth: { persistSession: false } },
);

async function getCompanyIdFromSession(): Promise<string | null> {
  try {
    const cookieStore = await cookies();

    const adminSession = await getIronSession<AdminSessionData>(cookieStore, adminSessionOptions);
    if (adminSession.companyId) {
      return adminSession.companyId;
    }

    if (adminSession.adminId) {
      const { data } = await supabaseAdmin
        .from('users_v2')
        .select('company_id')
        .eq('id', adminSession.adminId)
        .single();

      if (data?.company_id) {
        return data.company_id;
      }
    }

    const userSession = await getIronSession<SessionData>(cookieStore, sessionOptions);
    if (userSession.userId) {
      const { data } = await supabaseAdmin
        .from('users_v2')
        .select('company_id')
        .eq('id', userSession.userId)
        .single();

      if (data?.company_id) {
        return data.company_id;
      }
    }

    return null;
  } catch (error: unknown) {
    log.error('[PreviewChange] Error getting company_id', errorLogFields(error));
    return null;
  }
}

export async function POST(request: NextRequest) {
  try {
    const companyId = await getCompanyIdFromSession();

    if (!companyId) {
      return apiError('Não autorizado. Faça login novamente.', {
        request,
        status: 401,
      });
    }

    const body = await request.json();
    const internalAuthHeaders = await getOptionalInternalAuthHeaders({ companyId });
    const adminApiKeyResult = getAdminApiKeyOrResponse(request);
    if (adminApiKeyResult.response) return adminApiKeyResult.response;

    const response = await fetch(
      `${BACKEND_URL}/api/billing/preview-change?company_id=${companyId}`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Admin-API-Key': adminApiKeyResult.adminApiKey,
          ...internalAuthHeaders,
        },
        body: JSON.stringify(body),
      },
    );

    if (!response.ok) {
      log.warn('[PreviewChange API] Backend returned non-success', { status: response.status });
      return apiError('Erro ao simular alteração de plano', {
        request,
        status: response.status,
      });
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error: unknown) {
    log.error('[PreviewChange API] Error', errorLogFields(error));
    return apiError('Erro interno', {
      cause: error,
      logMessage: '[PreviewChange API] Request failed',
      request,
      status: 500,
    });
  }
}
