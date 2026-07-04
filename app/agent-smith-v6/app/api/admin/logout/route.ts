import { NextRequest, NextResponse } from 'next/server';
import { cookies } from 'next/headers';
import { getIronSession } from 'iron-session';
import { logSystemAction, getClientInfo } from '@/lib/logger';
import { adminSessionOptions, AdminSessionData } from '@/lib/iron-session';

export const dynamic = 'force-dynamic';

export async function POST(request: NextRequest) {
  const { ipAddress, userAgent } = getClientInfo(request);

  try {
    const cookieStore = await cookies();
    const session = await getIronSession<AdminSessionData>(cookieStore, adminSessionOptions);
    const adminId = session.adminId;

    // Destroy the session
    session.destroy();

    const response = NextResponse.json(
      { success: true, message: 'Logout realizado com sucesso' },
      { status: 200 },
    );

    response.cookies.delete('smith_admin_session');

    await logSystemAction({
      adminId,
      actionType: 'ADMIN_LOGOUT',
      resourceType: 'admin_session',
      details: { message: 'Admin logged out' },
      ipAddress,
      userAgent,
      status: 'success',
    });

    return response;
  } catch (error) {
    console.error('Admin logout error:', error);

    await logSystemAction({
      actionType: 'ERROR_OCCURRED',
      details: { error: String(error), endpoint: '/api/admin/logout' },
      ipAddress,
      userAgent,
      status: 'error',
      errorMessage: 'Erro ao fazer logout',
    });

    return NextResponse.json({ success: false, error: 'Erro ao fazer logout' }, { status: 500 });
  }
}
