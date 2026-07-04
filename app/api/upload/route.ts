import { NextRequest, NextResponse } from 'next/server';
import { getUserOrAdminSession } from '@/lib/auth-actions';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

// =============================================
// CONFIGURAÇÕES DE SEGURANÇA (alinhado com Supabase Storage)
// =============================================
const MAX_FILE_SIZE = 5 * 1024 * 1024; // 5MB (igual ao bucket chat-media)

const ALLOWED_BUCKETS = ['chat-media', 'attachments', 'avatars', 'voice-messages'];

const ALLOWED_MIME_TYPES: Record<string, string[]> = {
  'chat-media': ['image/jpeg', 'image/png', 'image/gif', 'image/webp'],
  attachments: ['image/jpeg', 'image/png', 'image/gif', 'image/webp', 'application/pdf'],
  avatars: ['image/jpeg', 'image/png', 'image/webp'],
  'voice-messages': ['audio/webm', 'audio/ogg', 'audio/mp3', 'audio/mpeg', 'audio/wav'],
};

function normalizeClientPath(path: string): string | null {
  const normalized = path.trim().replace(/^\/+|\/+$/g, '');

  if (!normalized) return '';
  if (normalized.includes('\\')) return null;
  if (normalized.split('/').some((segment) => !segment || segment === '.' || segment === '..')) {
    return null;
  }

  return normalized;
}

/**
 * POST /api/upload
 *
 * Uploads a file to Supabase Storage with security validations.
 * Requires: smith_user_session OR smith_admin_session cookie
 */
export async function POST(request: NextRequest) {
  try {
    // =============================================
    // AUTHENTICATION CHECK
    // =============================================
    const auth = await getUserOrAdminSession();
    if (auth.response) return auth.response;

    // =============================================
    // PARSE FORM DATA
    // =============================================
    const formData = await request.formData();
    const file = formData.get('file') as File | null;
    const bucket = (formData.get('bucket') as string) || 'attachments';
    const requestedPathPrefix = (formData.get('path') as string) || '';

    if (!file) {
      return NextResponse.json({ error: 'Arquivo não fornecido' }, { status: 400 });
    }

    // =============================================
    // VALIDAÇÃO: TAMANHO DO ARQUIVO
    // =============================================
    if (file.size > MAX_FILE_SIZE) {
      const maxMB = MAX_FILE_SIZE / 1024 / 1024;
      return NextResponse.json(
        { error: `Arquivo muito grande. Máximo permitido: ${maxMB}MB` },
        { status: 413 },
      );
    }

    // =============================================
    // VALIDAÇÃO: BUCKET PERMITIDO
    // =============================================
    if (!ALLOWED_BUCKETS.includes(bucket)) {
      return NextResponse.json({ error: 'Bucket não permitido' }, { status: 400 });
    }

    const sessionScope = auth.userSession?.userId || auth.adminSession?.companyId || auth.adminSession?.adminId;
    if (!sessionScope) {
      return NextResponse.json({ error: 'Escopo de upload ausente' }, { status: 403 });
    }

    const pathPrefix = normalizeClientPath(requestedPathPrefix);
    if (pathPrefix === null) {
      return NextResponse.json({ error: 'Caminho de upload inválido' }, { status: 400 });
    }

    const scopedPrefix =
      pathPrefix && (pathPrefix === sessionScope || pathPrefix.startsWith(`${sessionScope}/`))
        ? pathPrefix
        : !pathPrefix
          ? sessionScope
          : null;

    if (!scopedPrefix) {
      return NextResponse.json({ error: 'Caminho de upload não autorizado' }, { status: 403 });
    }

    // =============================================
    // VALIDAÇÃO: TIPO DE ARQUIVO
    // =============================================
    const allowedTypes = ALLOWED_MIME_TYPES[bucket] || [];
    if (!allowedTypes.includes(file.type)) {
      return NextResponse.json(
        { error: `Tipo de arquivo não permitido. Aceitos: ${allowedTypes.join(', ')}` },
        { status: 415 },
      );
    }

    // =============================================
    // SERVICE ROLE CLIENT (necessário para upload)
    // =============================================
    const supabaseAdmin = getSupabaseAdmin();

    // =============================================
    // GENERATE UNIQUE FILENAME
    // =============================================
    const timestamp = Date.now();
    const randomId = crypto.randomUUID();
    const extension = file.name.split('.').pop()?.toLowerCase() || 'bin';
    const fileName = `${timestamp}_${randomId}.${extension}`;
    const filePath = `${scopedPrefix}/${fileName}`;

    // =============================================
    // UPLOAD FILE
    // =============================================
    const arrayBuffer = await file.arrayBuffer();
    const buffer = new Uint8Array(arrayBuffer);

    const { data, error: uploadError } = await supabaseAdmin.storage
      .from(bucket)
      .upload(filePath, buffer, {
        contentType: file.type,
        upsert: false,
      });

    if (uploadError) {
      console.error('[UPLOAD API] Error uploading file:', uploadError);
      return NextResponse.json({ error: 'Erro ao fazer upload do arquivo' }, { status: 500 });
    }

    // =============================================
    // GET PUBLIC URL
    // =============================================
    const {
      data: { publicUrl },
    } = supabaseAdmin.storage.from(bucket).getPublicUrl(filePath);

    return NextResponse.json(
      {
        success: true,
        filePath: data.path,
        publicUrl,
        fileName: file.name,
        mimeType: file.type,
        size: file.size,
      },
      { status: 201 },
    );
  } catch (error: any) {
    console.error('[UPLOAD API] Error:', error);
    return NextResponse.json({ error: 'Erro interno ao fazer upload' }, { status: 500 });
  }
}
