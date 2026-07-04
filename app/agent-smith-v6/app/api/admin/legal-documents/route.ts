import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import { getIronSession } from 'iron-session';
import { adminSessionOptions, AdminSessionData } from '@/lib/iron-session';

const supabaseAdmin = createClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.SUPABASE_SERVICE_ROLE_KEY!,
    { auth: { persistSession: false } },
);

async function getMasterAdminSession(request: NextRequest): Promise<AdminSessionData | null> {
    try {
        const res = new Response();
        const session = await getIronSession<AdminSessionData>(request, res, adminSessionOptions);
        if (!session.adminId) return null;
        if (session.expiresAt && new Date(session.expiresAt) < new Date()) return null;

        // Verify it's a master admin (exists in admin_users table)
        const { data: admin } = await supabaseAdmin
            .from('admin_users')
            .select('id')
            .eq('id', session.adminId)
            .maybeSingle();

        if (!admin) return null;
        return session;
    } catch {
        return null;
    }
}

// GET - List all legal documents (Master Admin only)
export async function GET(request: NextRequest) {
    const session = await getMasterAdminSession(request);
    if (!session) {
        return NextResponse.json({ error: 'Acesso não autorizado' }, { status: 403 });
    }

    try {
        const { data, error } = await supabaseAdmin
            .from('legal_documents')
            .select('*')
            .order('type', { ascending: true })
            .order('created_at', { ascending: false });

        if (error) {
            console.error('[LEGAL DOCS] Error fetching:', error.message);
            return NextResponse.json({ error: 'Erro ao buscar documentos' }, { status: 500 });
        }

        return NextResponse.json({ documents: data });
    } catch (error) {
        console.error('[LEGAL DOCS] Unexpected error:', error);
        return NextResponse.json({ error: 'Erro interno do servidor' }, { status: 500 });
    }
}

// POST - Create a new legal document (Master Admin only)
export async function POST(request: NextRequest) {
    const session = await getMasterAdminSession(request);
    if (!session) {
        return NextResponse.json({ error: 'Acesso não autorizado' }, { status: 403 });
    }

    try {
        const body = await request.json();
        const { type, title, content, version, is_active } = body;

        if (!type || !title || !content || !version) {
            return NextResponse.json({ error: 'Todos os campos são obrigatórios' }, { status: 400 });
        }

        if (!['terms_of_use', 'privacy_policy'].includes(type)) {
            return NextResponse.json({ error: 'Tipo inválido' }, { status: 400 });
        }

        // If activating this document, deactivate others of the same type
        if (is_active) {
            await supabaseAdmin
                .from('legal_documents')
                .update({ is_active: false })
                .eq('type', type)
                .eq('is_active', true);
        }

        const { data, error } = await supabaseAdmin
            .from('legal_documents')
            .insert({
                type,
                title,
                content,
                version,
                is_active: is_active || false,
                created_by: session.adminId,
            })
            .select()
            .single();

        if (error) {
            console.error('[LEGAL DOCS] Error creating:', error.message);
            return NextResponse.json({ error: 'Erro ao criar documento' }, { status: 500 });
        }

        return NextResponse.json({ document: data }, { status: 201 });
    } catch (error) {
        console.error('[LEGAL DOCS] Unexpected error:', error);
        return NextResponse.json({ error: 'Erro interno do servidor' }, { status: 500 });
    }
}
