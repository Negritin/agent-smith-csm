/**
 * DELETE /api/chat/session
 * 
 * Proxy to FastAPI backend to clear expired session memory.
 * Called by widget frontend when session TTL (24h) expires.
 */

import { NextRequest, NextResponse } from 'next/server';
import { getUserOrAdminSession } from '@/lib/auth-actions';
import { apiError } from '@/lib/api-error';
import { getAdminApiKeyOrResponse } from '@/lib/admin-proxy';
import {
    createInternalAuthHeadersForAdminSession,
    createInternalAuthHeadersForUserSession,
} from '@/lib/internal-jwt';
import { errorLogFields, log } from '@/lib/logger';

const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

export async function DELETE(request: NextRequest) {
    try {
        const auth = await getUserOrAdminSession();
        if (auth.response) {
            return apiError('Não autorizado', { request, status: auth.response.status || 401 });
        }

        const body = await request.json();
        const { sessionId } = body;
        const companyId = auth.userSession?.companyId || auth.adminSession?.companyId || null;

        if (auth.adminSession?.role === 'master_admin') {
            return apiError('companyId must be derived from a tenant session', {
                request,
                status: 403,
            });
        }

        if (!sessionId || !companyId) {
            return apiError('sessionId and companyId are required', {
                request,
                status: 400,
            });
        }

        const internalAuthHeaders = auth.userSession
            ? createInternalAuthHeadersForUserSession(auth.userSession, companyId)
            : createInternalAuthHeadersForAdminSession(auth.adminSession, companyId);

        const adminApiKeyResult = getAdminApiKeyOrResponse(request);
        if (adminApiKeyResult.response) return adminApiKeyResult.response;

        // Proxy to FastAPI backend
        const response = await fetch(`${BACKEND_URL}/chat/session`, {
            method: 'DELETE',
            headers: {
                'Content-Type': 'application/json',
                'X-Admin-API-Key': adminApiKeyResult.adminApiKey,
                ...internalAuthHeaders,
            },
            body: JSON.stringify({ sessionId, companyId }),
        });

        const data = await response.json();

        if (!response.ok) {
            log.warn('[API] Backend failed to delete chat session', { status: response.status });
            return apiError('Failed to delete session', {
                request,
                status: response.status,
            });
        }

        return NextResponse.json(data);

    } catch (error: unknown) {
        log.error('[API] Error deleting session', errorLogFields(error));
        return apiError('Failed to delete session', {
            cause: error,
            logMessage: '[API] Delete chat session failed',
            request,
            status: 500,
        });
    }
}
