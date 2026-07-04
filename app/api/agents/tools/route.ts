import { NextRequest } from 'next/server';
import { apiError } from '@/lib/api-error';

export const dynamic = 'force-dynamic';

function legacyToolsRouteGone(request: NextRequest) {
  return apiError('Endpoint descontinuado. Use o proxy administrativo seguro.', {
    request,
    status: 410,
  });
}

export const GET = legacyToolsRouteGone;
export const POST = legacyToolsRouteGone;
export const PUT = legacyToolsRouteGone;
export const DELETE = legacyToolsRouteGone;
