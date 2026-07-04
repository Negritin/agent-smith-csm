import { NextRequest, NextResponse } from 'next/server';
import { getAdminApiKeyOrResponse } from '@/lib/admin-proxy';
import { requireMasterAdminSession } from '@/lib/auth-actions';

const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

export async function PUT(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  try {
    const auth = await requireMasterAdminSession();
    if (auth.response) return auth.response;

    const adminApiKey = getAdminApiKeyOrResponse();
    if (adminApiKey.response) return adminApiKey.response;

    const body = await request.json();
    const { id } = await params;

    const response = await fetch(`${BACKEND_URL}/api/admin/pricing/${id}`, {
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
    console.error('[Pricing API] Update error:', error);
    return NextResponse.json(
      { success: false, error: 'Failed to update pricing' },
      { status: 500 },
    );
  }
}
