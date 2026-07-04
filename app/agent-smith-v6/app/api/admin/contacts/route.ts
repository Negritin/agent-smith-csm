import { NextRequest } from 'next/server';
import { apiError } from '@/lib/api-error';
import { requireAttendanceAdmin } from '@/lib/attendance-actions';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

export const dynamic = 'force-dynamic';

const supabaseAdmin = getSupabaseAdmin();

const DEFAULT_PAGE_SIZE = 50;
const MAX_PAGE_SIZE = 200;
const ALLOWED_CHANNELS = new Set(['whatsapp', 'widget', 'web']);

type ContactRow = {
  contact_key: string;
  user_id: string | null;
  name: string | null;
  phone: string | null;
  email: string | null;
  channel: string | null;
  created_at: string | null;
  last_seen: string | null;
  conversation_count: number;
  total_count: number;
};

/**
 * `created_to` chega como data pura `YYYY-MM-DD` do <input type="date">. A RPC
 * usa o limite superior EXCLUSIVO `c.created_at < p_created_to`, e o Postgres
 * faz cast da data pura p/ 00:00 daquele dia — então "Criado até 2026-06-27"
 * excluiria TODO o dia 27 (e from=to=mesmo dia devolveria 0). Avançamos p/ a
 * meia-noite do DIA SEGUINTE (mantém o `<` da RPC, agora inclusivo do dia
 * escolhido), preservando a data pura p/ casar a semântica de TZ-de-sessão do
 * limite inferior `created_from`. (UTC só p/ aritmética de calendário estável.)
 */
function inclusiveCreatedTo(value: string | null): string | null {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  const d = new Date(`${value}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + 1);
  return d.toISOString().slice(0, 10);
}

/**
 * Lê e normaliza os filtros compartilhados por list + export.
 *
 * Não exportado de propósito: Next valida os exports de um `route.ts` contra a
 * lista de campos de rota conhecidos (GET/POST/dynamic/...) ao gerar
 * `.next/types`, então exportar um helper daqui quebraria `next build`. O route
 * de export reimplementa a MESMA semântica localmente (SPEC §1.3 permite).
 */
function parseContactFilters(params: URLSearchParams) {
  const search = (params.get('search') || '').trim() || null;
  const channelRaw = (params.get('channel') || '').toLowerCase();
  const channel = ALLOWED_CHANNELS.has(channelRaw) ? channelRaw : null;
  const createdFrom = params.get('created_from') || null;
  const createdTo = inclusiveCreatedTo(params.get('created_to') || null);
  return { search, channel, createdFrom, createdTo };
}

/**
 * GET /api/admin/contacts (SPEC §1.3)
 *
 * Lista paginada de contatos derivados de `conversations` via rpc_list_contacts.
 * Modelo de paginação ÚNICO: a RPC devolve items + total_count numa só passada
 * (COUNT(*) OVER()). NÃO usar count:'exact' + .range() (B2).
 * Tenant scoping vive DENTRO da RPC (company_id no WHERE e nos JOINs).
 */
export async function GET(request: NextRequest) {
  try {
    const authResult = await requireAttendanceAdmin(request);
    if (authResult.response) return authResult.response;
    const companyId = authResult.auth.companyId;

    const params = new URL(request.url).searchParams;
    const { search, channel, createdFrom, createdTo } = parseContactFilters(params);

    const page = Math.max(1, parseInt(params.get('page') || '1', 10) || 1);
    const pageSize = Math.min(
      MAX_PAGE_SIZE,
      Math.max(
        1,
        parseInt(params.get('page_size') || String(DEFAULT_PAGE_SIZE), 10) || DEFAULT_PAGE_SIZE,
      ),
    );
    const offset = (page - 1) * pageSize;

    const { data, error } = await supabaseAdmin.rpc('rpc_list_contacts', {
      p_company_id: companyId,
      p_search: search,
      p_channel: channel,
      p_created_from: createdFrom,
      p_created_to: createdTo,
      p_limit: pageSize,
      p_offset: offset,
    });

    if (error) {
      return apiError('Erro ao listar contatos', {
        request,
        status: 500,
        cause: error,
        logMessage: '[contacts] rpc_list_contacts failed',
      });
    }

    const rows = (data ?? []) as ContactRow[];
    const total = rows.length > 0 ? Number(rows[0].total_count) : 0;

    const items = rows.map((r) => ({
      contact_key: r.contact_key,
      user_id: r.user_id,
      name: r.name,
      phone: r.phone,
      email: r.email,
      channel: r.channel,
      created_at: r.created_at,
      last_seen: r.last_seen,
      conversation_count: Number(r.conversation_count),
    }));

    return Response.json({ items, total, page, page_size: pageSize });
  } catch (err) {
    return apiError('Erro ao listar contatos', {
      request,
      status: 500,
      cause: err,
      logMessage: '[contacts] unexpected error',
    });
  }
}
