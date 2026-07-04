import { NextRequest, NextResponse } from 'next/server';

export function GET(request: NextRequest) {
  const redirectUrl = new URL('/admin/billing', request.url);
  const sessionId = request.nextUrl.searchParams.get('session_id');

  redirectUrl.searchParams.set('checkout', 'success');
  if (sessionId) {
    redirectUrl.searchParams.set('session_id', sessionId);
  }

  return NextResponse.redirect(redirectUrl);
}
