/**
 * Sanitization Proxy - Upload
 *
 * POST /api/sanitization/upload
 * Validates iron-session, then forwards multipart upload to Python backend.
 *
 * Auth logic:
 * - Master admin: uses company_id from frontend (manages multiple companies)
 * - Company admin: forces company_id from session (security)
 */
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
import { getAdminApiKeyOrResponse } from '@/lib/admin-proxy';
import { apiError } from '@/lib/api-error';
import { getOptionalInternalAuthHeaders } from '@/lib/internal-jwt';
import { errorLogFields, log } from '@/lib/logger';
import { auditMasterAdminCompanyOverride } from '@/lib/security-audit';

const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || process.env.NEXT_PUBLIC_BACKEND_URL || 'http://localhost:8000';

const supabaseAdmin = createClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.SUPABASE_SERVICE_ROLE_KEY!,
    { auth: { persistSession: false } },
);

/**
 * Resolves company_id based on session + optional frontend param.
 * - Master admin: trusts frontendCompanyId (they manage multiple companies)
 * - Company admin: returns their own company_id (ignores frontend param)
 * - User session: returns their own company_id
 * Returns null if not authenticated.
 */
async function resolveCompanyId(
    frontendCompanyId: string | null | undefined,
    request: NextRequest,
): Promise<string | null> {
    try {
        const cookieStore = await cookies();

        // 1. Check admin session
        const adminSession = await getIronSession<AdminSessionData>(cookieStore, adminSessionOptions);

        if (adminSession.adminId) {
            if (adminSession.role === 'master_admin') {
                // Master admin: use frontendCompanyId (they manage multiple companies)
                if (frontendCompanyId) {
                    await auditMasterAdminCompanyOverride({
                        request,
                        actorId: adminSession.adminId,
                        sessionCompanyId: adminSession.companyId || null,
                        frontendCompanyId,
                        resourceType: 'sanitization_jobs',
                        resourceId: frontendCompanyId,
                        action: 'sanitization_upload',
                    });
                    return frontendCompanyId;
                }
                // Fallback: try their own company
                if (adminSession.companyId) return adminSession.companyId;
                return null;
            }

            // Company admin: use their own company_id (security)
            if (adminSession.companyId) return adminSession.companyId;

            // Lookup from DB
            const { data } = await supabaseAdmin
                .from('users_v2')
                .select('company_id')
                .eq('id', adminSession.adminId)
                .single();

            if (data?.company_id) return data.company_id;
        }

        // 2. Check user session
        const userSession = await getIronSession<SessionData>(cookieStore, sessionOptions);
        if (userSession.userId) {
            if (userSession.companyId) return userSession.companyId;

            const { data } = await supabaseAdmin
                .from('users_v2')
                .select('company_id')
                .eq('id', userSession.userId)
                .single();

            if (data?.company_id) return data.company_id;
        }

        return null;
    } catch (error: unknown) {
        log.error('[Sanitization Upload] Error resolving company_id', errorLogFields(error));
        return null;
    }
}

export async function POST(request: NextRequest) {
    try {
        const incomingFormData = await request.formData();

        // Extract company_id from form data (sent by frontend for master admin)
        const frontendCompanyId = incomingFormData.get('company_id') as string | null;

        const companyId = await resolveCompanyId(frontendCompanyId, request);

        if (!companyId) {
            return apiError('Authentication required. Please log in.', {
                request,
                status: 401,
            });
        }

        // Build new form data with resolved company_id
        const newFormData = new FormData();

        incomingFormData.forEach((value, key) => {
            if (key !== 'company_id') {
                newFormData.append(key, value);
            }
        });

        // Inject the resolved company_id (trusted)
        newFormData.append('company_id', companyId);

        const adminApiKey = getAdminApiKeyOrResponse(request);
        if (adminApiKey.response) return adminApiKey.response;
        const internalAuthHeaders = await getOptionalInternalAuthHeaders({ companyId });

        const response = await fetch(`${BACKEND_URL}/api/sanitization/upload`, {
            method: 'POST',
            headers: {
                'X-Admin-API-Key': adminApiKey.adminApiKey,
                ...internalAuthHeaders,
            },
            body: newFormData,
        });

        if (!response.ok) {
            log.warn('[Sanitization Upload API] Backend returned non-success', {
                status: response.status,
            });
            return apiError('Erro ao enviar documento para sanitização', {
                request,
                status: response.status,
            });
        }

        const data = await response.json();
        return NextResponse.json(data);
    } catch (error: unknown) {
        log.error('[Sanitization Upload API] Error', errorLogFields(error));
        return apiError('Erro interno', {
            cause: error,
            logMessage: '[Sanitization Upload API] Request failed',
            request,
            status: 500,
        });
    }
}
