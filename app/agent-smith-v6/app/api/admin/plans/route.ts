import { NextRequest, NextResponse } from 'next/server';
import { getAdminApiKeyOrResponse } from '@/lib/admin-proxy';
import { requireMasterAdminSession } from '@/lib/auth-actions';

const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

export async function GET(request: NextRequest) {
  try {
    const auth = await requireMasterAdminSession();
    if (auth.response) return auth.response;

    const adminApiKey = getAdminApiKeyOrResponse();
    if (adminApiKey.response) return adminApiKey.response;

    const { searchParams } = new URL(request.url);
    const includeInactive = searchParams.get('include_inactive') === 'true';

    const url = `${BACKEND_URL}/api/admin/plans${includeInactive ? '?include_inactive=true' : ''}`;

    const response = await fetch(url, {
      headers: {
        'Content-Type': 'application/json',
        'X-Admin-API-Key': adminApiKey.adminApiKey,
      },
    });

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('[Plans API] Error:', error);
    return NextResponse.json({ success: false, data: [], count: 0 }, { status: 500 });
  }
}

export async function POST(request: NextRequest) {
  try {
    const auth = await requireMasterAdminSession();
    if (auth.response) return auth.response;

    const adminApiKey = getAdminApiKeyOrResponse();
    if (adminApiKey.response) return adminApiKey.response;

    const body = await request.json();

    const response = await fetch(`${BACKEND_URL}/api/admin/plans`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Admin-API-Key': adminApiKey.adminApiKey,
      },
      body: JSON.stringify(body),
    });

    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    console.error('[Plans API] Create error:', error);
    return NextResponse.json({ success: false, error: 'Failed to create plan' }, { status: 500 });
  }
}
