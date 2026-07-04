import { NextRequest, NextResponse } from 'next/server';
import { cookies } from 'next/headers';
import { getIronSession } from 'iron-session';
import { logSystemAction, getClientInfo } from '@/lib/logger';
import { sessionOptions, SessionData } from '@/lib/iron-session';

export const dynamic = 'force-dynamic';

export async function POST(request: NextRequest) {
  const { ipAddress, userAgent } = getClientInfo(request);

  try {
    // Ler sessão atual para logging
    const cookieStore = await cookies();
    const session = await getIronSession<SessionData>(cookieStore, sessionOptions);

    const userId = session.userId;
    const companyId = session.companyId;

    // Destruir sessão (limpa o cookie criptografado)
    session.destroy();

    await logSystemAction({
      userId,
      companyId: companyId ?? undefined,
      actionType: 'LOGOUT',
      resourceType: 'session',
      details: { message: 'User logged out' },
      ipAddress,
      userAgent,
      status: 'success',
    });

    return NextResponse.json(
      { success: true, message: 'Logout realizado com sucesso' },
      { status: 200 },
    );
  } catch (error) {
    console.error('Logout error:', error);

    await logSystemAction({
      actionType: 'ERROR_OCCURRED',
      details: { error: String(error), endpoint: '/api/auth/logout' },
      ipAddress,
      userAgent,
      status: 'error',
      errorMessage: 'Erro ao fazer logout',
    });

    return NextResponse.json({ success: false, error: 'Erro ao fazer logout' }, { status: 500 });
  }
}
