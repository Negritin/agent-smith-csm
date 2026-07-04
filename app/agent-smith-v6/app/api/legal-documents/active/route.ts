import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';

const supabaseAdmin = createClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.SUPABASE_SERVICE_ROLE_KEY!,
    { auth: { persistSession: false } },
);

// GET - Fetch active legal documents (PUBLIC - no auth required)
// Used by registration page to display terms content
// Query params: ?type=terms_of_use or ?type=privacy_policy
export async function GET(request: NextRequest) {
    try {
        const { searchParams } = new URL(request.url);
        const type = searchParams.get('type');

        let query = supabaseAdmin
            .from('legal_documents')
            .select('id, type, title, content, version, updated_at')
            .eq('is_active', true);

        if (type) {
            if (!['terms_of_use', 'privacy_policy'].includes(type)) {
                return NextResponse.json({ error: 'Tipo inválido' }, { status: 400 });
            }
            query = query.eq('type', type);
        }

        const { data, error } = await query;

        if (error) {
            console.error('[LEGAL DOCS PUBLIC] Error fetching:', error.message);
            return NextResponse.json({ error: 'Erro ao buscar documentos' }, { status: 500 });
        }

        // If requesting a specific type, return single document
        if (type) {
            return NextResponse.json({ document: data?.[0] || null });
        }

        // Otherwise return all active documents
        return NextResponse.json({ documents: data });
    } catch (error) {
        console.error('[LEGAL DOCS PUBLIC] Unexpected error:', error);
        return NextResponse.json({ error: 'Erro interno do servidor' }, { status: 500 });
    }
}
