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

// PUT - Update a legal document
export async function PUT(
    request: NextRequest,
    { params }: { params: Promise<{ id: string }> },
) {
    const session = await getMasterAdminSession(request);
    if (!session) {
        return NextResponse.json({ error: 'Acesso não autorizado' }, { status: 403 });
    }

    try {
        const { id } = await params;
        const body = await request.json();
        const { type, title, content, version, is_active } = body;

        if (!type || !title || !content || !version) {
            return NextResponse.json({ error: 'Todos os campos são obrigatórios' }, { status: 400 });
        }

        // If activating this document, deactivate others of the same type
        if (is_active) {
            await supabaseAdmin
                .from('legal_documents')
                .update({ is_active: false })
                .eq('type', type)
                .eq('is_active', true)
                .neq('id', id);
        }

        const { data, error } = await supabaseAdmin
            .from('legal_documents')
            .update({
                type,
                title,
                content,
                version,
                is_active: is_active || false,
            })
            .eq('id', id)
            .select()
            .single();

        if (error) {
            console.error('[LEGAL DOCS] Error updating:', error.message);
            return NextResponse.json({ error: 'Erro ao atualizar documento' }, { status: 500 });
        }

        if (!data) {
            return NextResponse.json({ error: 'Documento não encontrado' }, { status: 404 });
        }

        return NextResponse.json({ document: data });
    } catch (error) {
        console.error('[LEGAL DOCS] Unexpected error:', error);
        return NextResponse.json({ error: 'Erro interno do servidor' }, { status: 500 });
    }
}

// DELETE - Delete a legal document
export async function DELETE(
    request: NextRequest,
    { params }: { params: Promise<{ id: string }> },
) {
    const session = await getMasterAdminSession(request);
    if (!session) {
        return NextResponse.json({ error: 'Acesso não autorizado' }, { status: 403 });
    }

    try {
        const { id } = await params;

        const { error } = await supabaseAdmin
            .from('legal_documents')
            .delete()
            .eq('id', id);

        if (error) {
            console.error('[LEGAL DOCS] Error deleting:', error.message);
            return NextResponse.json({ error: 'Erro ao excluir documento' }, { status: 500 });
        }

        return NextResponse.json({ success: true });
    } catch (error) {
        console.error('[LEGAL DOCS] Unexpected error:', error);
        return NextResponse.json({ error: 'Erro interno do servidor' }, { status: 500 });
    }
}
