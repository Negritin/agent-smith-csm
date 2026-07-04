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

const supabaseAdmin = createClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.SUPABASE_SERVICE_ROLE_KEY!,
    { auth: { persistSession: false } },
);

/**
 * POST /api/user/accept-terms
 *
 * Updates the user's accepted_terms_version and terms_accepted_at
 * Requires authenticated session (user or company admin)
 */
export async function POST(request: NextRequest) {
    try {
        const cookieStore = await cookies();

        // Get user ID from session
        let userId: string | null = null;

        const userSession = await getIronSession<SessionData>(cookieStore, sessionOptions);
        if (userSession.userId) {
            userId = userSession.userId;
        } else {
            const adminSession = await getIronSession<AdminSessionData>(cookieStore, adminSessionOptions);
            if (adminSession.adminId) {
                userId = adminSession.adminId;
            }
        }

        if (!userId) {
            return NextResponse.json({ error: 'Não autenticado' }, { status: 401 });
        }

        const body = await request.json();
        const { documentId } = body;

        if (!documentId) {
            return NextResponse.json({ error: 'documentId é obrigatório' }, { status: 400 });
        }

        // Verify this is the active terms_of_use document
        const { data: activeDoc } = await supabaseAdmin
            .from('legal_documents')
            .select('id')
            .eq('id', documentId)
            .eq('type', 'terms_of_use')
            .eq('is_active', true)
            .maybeSingle();

        if (!activeDoc) {
            return NextResponse.json({ error: 'Documento inválido ou não ativo' }, { status: 400 });
        }

        // Update user's accepted terms version
        const { error } = await supabaseAdmin
            .from('users_v2')
            .update({
                accepted_terms_version: documentId,
                terms_accepted_at: new Date().toISOString(),
            })
            .eq('id', userId);

        if (error) {
            console.error('[ACCEPT TERMS] Error updating:', error.message);
            return NextResponse.json({ error: 'Erro ao atualizar aceite' }, { status: 500 });
        }

        return NextResponse.json({ success: true });
    } catch (error) {
        console.error('[ACCEPT TERMS] Unexpected error:', error);
        return NextResponse.json({ error: 'Erro interno do servidor' }, { status: 500 });
    }
}
