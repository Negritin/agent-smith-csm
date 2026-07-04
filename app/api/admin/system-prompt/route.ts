import { NextRequest, NextResponse } from 'next/server';
import { getAdminApiKeyOrResponse } from '@/lib/admin-proxy';
import { requireMasterAdminSession } from '@/lib/auth-actions';

const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

// GET /api/admin/system-prompt — lê o system base prompt global (master admin)
export async function GET() {
  try {
    const auth = await requireMasterAdminSession();
    if (auth.response) return auth.response;

    const adminApiKey = getAdminApiKeyOrResponse();
    if (adminApiKey.response) return adminApiKey.response;

    const response = await fetch(`${BACKEND_URL}/api/admin/system-prompt`, {
      headers: {
        'Content-Type': 'application/json',
        'X-Admin-API-Key': adminApiKey.adminApiKey,
      },
      cache: 'no-store',
    });

    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    console.error('[System Prompt API] GET error:', error);
    return NextResponse.json({ detail: 'Erro ao carregar o system prompt' }, { status: 500 });
  }
}

// PUT /api/admin/system-prompt — atualiza (master admin). R1: não pode ser vazio.
export async function PUT(request: NextRequest) {
  try {
    const auth = await requireMasterAdminSession();
    if (auth.response) return auth.response;

    const adminApiKey = getAdminApiKeyOrResponse();
    if (adminApiKey.response) return adminApiKey.response;

    const body = await request.json();
    const value = (body?.value ?? '').toString();

    // R1 (espelho do servidor, que é a autoridade): bloqueia vazio antes de chamar o backend
    if (!value.trim()) {
      return NextResponse.json(
        { detail: 'O system prompt não pode ficar vazio.' },
        { status: 400 },
      );
    }

    const response = await fetch(`${BACKEND_URL}/api/admin/system-prompt`, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        'X-Admin-API-Key': adminApiKey.adminApiKey,
      },
      body: JSON.stringify({ value, updated_by: auth.session.adminId }),
    });

    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    console.error('[System Prompt API] PUT error:', error);
    return NextResponse.json({ detail: 'Erro ao salvar o system prompt' }, { status: 500 });
  }
}
