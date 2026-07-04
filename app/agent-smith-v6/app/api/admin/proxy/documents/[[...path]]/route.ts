import { NextRequest } from 'next/server';
import { authenticatedProxy } from '@/lib/admin-proxy';

export const dynamic = 'force-dynamic';

async function handler(
  request: NextRequest,
  { params }: { params: Promise<{ path?: string[] }> },
) {
  const { path } = await params;
  const backendPath = path ? `/documents/${path.join('/')}` : '/documents';
  return authenticatedProxy(request, backendPath);
}

export const GET = handler;
export const POST = handler;
export const DELETE = handler;
