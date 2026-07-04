import { NextRequest, NextResponse } from 'next/server';
import { getAdminApiKeyOrResponse } from '@/lib/admin-proxy';
import { requireMasterAdminSession } from '@/lib/auth-actions';

const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  try {
    const auth = await requireMasterAdminSession();
    if (auth.response) return auth.response;

    const adminApiKey = getAdminApiKeyOrResponse();
    if (adminApiKey.response) return adminApiKey.response;

    const { id } = await params;

    const response = await fetch(`${BACKEND_URL}/api/admin/plans/${id}`, {
      headers: {
        'Content-Type': 'application/json',
        'X-Admin-API-Key': adminApiKey.adminApiKey,
      },
    });

    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    console.error('[Plans API] Get error:', error);
    return NextResponse.json({ success: false, error: 'Failed to get plan' }, { status: 500 });
  }
}

export async function PUT(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  try {
    const auth = await requireMasterAdminSession();
    if (auth.response) return auth.response;

    const adminApiKey = getAdminApiKeyOrResponse();
    if (adminApiKey.response) return adminApiKey.response;

    const body = await request.json();
    const { id } = await params;

    const response = await fetch(`${BACKEND_URL}/api/admin/plans/${id}`, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        'X-Admin-API-Key': adminApiKey.adminApiKey,
      },
      body: JSON.stringify(body),
    });

    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    console.error('[Plans API] Update error:', error);
    return NextResponse.json({ success: false, error: 'Failed to update plan' }, { status: 500 });
  }
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const auth = await requireMasterAdminSession();
    if (auth.response) return auth.response;

    const adminApiKey = getAdminApiKeyOrResponse();
    if (adminApiKey.response) return adminApiKey.response;

    const { id } = await params;
    const { searchParams } = new URL(request.url);
    const hardDelete = searchParams.get('hard_delete') === 'true';

    const response = await fetch(`${BACKEND_URL}/api/admin/plans/${id}?hard_delete=${hardDelete}`, {
      method: 'DELETE',
      headers: {
        'Content-Type': 'application/json',
        'X-Admin-API-Key': adminApiKey.adminApiKey,
      },
    });

    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    console.error('[Plans API] Delete error:', error);
    return NextResponse.json({ success: false, error: 'Failed to delete plan' }, { status: 500 });
  }
}
