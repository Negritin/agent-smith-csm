import { NextRequest, NextResponse } from 'next/server';

export function GET(request: NextRequest) {
  const redirectUrl = new URL('/admin/billing', request.url);
  redirectUrl.searchParams.set('checkout', 'cancelled');

  return NextResponse.redirect(redirectUrl);
}
