/**
 * Sanitization Proxy - Download Sanitized File
 *
 * GET /api/sanitization/download/[jobId]?company_id=xxx
 * Validates iron-session, then streams the file from Python backend.
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

async function resolveCompanyId(
    frontendCompanyId: string | null | undefined,
    request: NextRequest,
    resourceId: string,
): Promise<string | null> {
    try {
        const cookieStore = await cookies();

        const adminSession = await getIronSession<AdminSessionData>(cookieStore, adminSessionOptions);
        if (adminSession.adminId) {
            if (adminSession.role === 'master_admin') {
                if (frontendCompanyId) {
                    await auditMasterAdminCompanyOverride({
                        request,
                        actorId: adminSession.adminId,
                        sessionCompanyId: adminSession.companyId || null,
                        frontendCompanyId,
                        resourceType: 'sanitization_jobs',
                        resourceId,
                        action: 'download_sanitization_job',
                    });
                    return frontendCompanyId;
                }
                if (adminSession.companyId) return adminSession.companyId;
                return null;
            }
            if (adminSession.companyId) return adminSession.companyId;
            const { data } = await supabaseAdmin
                .from('users_v2')
                .select('company_id')
                .eq('id', adminSession.adminId)
                .single();
            if (data?.company_id) return data.company_id;
        }

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
        log.error('[Sanitization Download] Error resolving company_id', errorLogFields(error));
        return null;
    }
}

export async function GET(
    request: NextRequest,
    { params }: { params: Promise<{ jobId: string }> },
) {
    try {
        const { jobId } = await params;
        const frontendCompanyId = request.nextUrl.searchParams.get('company_id');
        const companyId = await resolveCompanyId(frontendCompanyId, request, jobId);

        if (!companyId) {
            return apiError('Authentication required. Please log in.', {
                request,
                status: 401,
            });
        }

        const adminApiKey = getAdminApiKeyOrResponse(request);
        if (adminApiKey.response) return adminApiKey.response;
        const internalAuthHeaders = await getOptionalInternalAuthHeaders({ companyId });

        const response = await fetch(
            `${BACKEND_URL}/api/sanitization/download/${jobId}?company_id=${companyId}`,
            {
                headers: {
                    'X-Admin-API-Key': adminApiKey.adminApiKey,
                    ...internalAuthHeaders,
                },
            },
        );

        if (!response.ok) {
            log.warn('[Sanitization Download API] Backend returned non-success', {
                status: response.status,
            });
            return apiError('Erro ao baixar arquivo sanitizado', {
                request,
                status: response.status,
            });
        }

        // Stream the binary response back to the client
        const contentDisposition = response.headers.get('Content-Disposition');
        const contentType = response.headers.get('Content-Type') || 'text/markdown';

        const headers: Record<string, string> = {
            'Content-Type': contentType,
        };

        if (contentDisposition) {
            headers['Content-Disposition'] = contentDisposition;
        }

        const blob = await response.blob();
        return new NextResponse(blob, {
            status: 200,
            headers,
        });
    } catch (error: unknown) {
        log.error('[Sanitization Download API] Error', errorLogFields(error));
        return apiError('Erro interno', {
            cause: error,
            logMessage: '[Sanitization Download API] Request failed',
            request,
            status: 500,
        });
    }
}
