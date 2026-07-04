import { NextRequest } from 'next/server';
import { apiError } from '@/lib/api-error';
import { requireAttendanceAdmin } from '@/lib/attendance-actions';
import { getSupabaseAdmin } from '@/lib/supabase-admin';

export const dynamic = 'force-dynamic';

const supabaseAdmin = getSupabaseAdmin();

const ALLOWED_CHANNELS = new Set(['whatsapp', 'widget', 'web']);

const CSV_HEADER = ['Email', 'Telefone', 'Nome', 'Criado em', 'Nº de conversas'];

/**
 * `created_to` chega como data pura `YYYY-MM-DD` do <input type="date">. A RPC
 * usa o limite superior EXCLUSIVO `c.created_at < p_created_to`, e o Postgres
 * faz cast da data pura p/ 00:00 daquele dia — então "Criado até 2026-06-27"
 * excluiria TODO o dia 27 (e from=to=mesmo dia devolveria 0). Avançamos p/ a
 * meia-noite do DIA SEGUINTE (mantém o `<` da RPC, agora inclusivo do dia
 * escolhido), preservando a data pura p/ casar a semântica de TZ-de-sessão do
 * limite inferior `created_from`. (UTC só p/ aritmética de calendário estável.)
 * MESMA normalização do route de lista — list e CSV precisam concordar (D7).
 */
function inclusiveCreatedTo(value: string | null): string | null {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  const d = new Date(`${value}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + 1);
  return d.toISOString().slice(0, 10);
}

/**
 * Lê e normaliza os filtros — MESMA semântica do route de lista
 * (`app/api/admin/contacts/route.ts`). Reimplementado localmente de propósito:
 * Next valida os exports de um `route.ts` ao gerar `.next/types`, então importar
 * um helper de um route irmão quebraria `next build` (SPEC §1.3 permite a
 * reimplementação local para manter uma única semântica).
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
 * Escapa um campo para CSV (RFC 4180): aspas duplas + duplica aspas internas.
 * Também neutraliza CSV formula injection (OWASP CSV Injection / CWE-1236):
 * Excel/LibreOffice avaliam como FÓRMULA toda célula cujo 1º caractere seja
 * `= + - @`, TAB (\t) ou CR (\r) — mesmo dentro de aspas, pois o parser remove
 * as aspas externas antes de avaliar. Os campos exportados sofrem influência do
 * usuário final (`user_name` do WhatsApp/widget) e telefones começam com `+`
 * (ex.: +5511999...), então isso dispara em dado comum, não só malicioso.
 * Prefixamos com `'` (apóstrofo de "forçar texto") antes do escaping de aspas.
 */
function csvCell(value: unknown): string {
  let s = value == null ? '' : String(value);
  if (/^[=+\-@\t\r]/.test(s) || /^\s*[=+\-@]/.test(s)) {
    s = `'${s}`;
  }
  return `"${s.replace(/"/g, '""')}"`;
}

function formatDate(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString('pt-BR');
}

type ExportRow = {
  email: string | null;
  phone: string | null;
  name: string | null;
  created_at: string | null;
  conversation_count: number;
};

/**
 * GET /api/admin/contacts/export (SPEC §1.3 / D7)
 *
 * CSV server-side do conjunto INTEIRO do filtro atual (mesma RPC, p_limit=NULL).
 * Mesmos query params da lista. NÃO gerar CSV no cliente (C1/D7).
 */
export async function GET(request: NextRequest) {
  try {
    const authResult = await requireAttendanceAdmin(request);
    if (authResult.response) return authResult.response;
    const companyId = authResult.auth.companyId;

    const params = new URL(request.url).searchParams;
    const { search, channel, createdFrom, createdTo } = parseContactFilters(params);

    const { data, error } = await supabaseAdmin.rpc('rpc_list_contacts', {
      p_company_id: companyId,
      p_search: search,
      p_channel: channel,
      p_created_from: createdFrom,
      p_created_to: createdTo,
      p_limit: null, // sem janela => conjunto inteiro do filtro
      p_offset: 0,
    });

    if (error) {
      return apiError('Erro ao exportar contatos', {
        request,
        status: 500,
        cause: error,
        logMessage: '[contacts/export] rpc_list_contacts failed',
      });
    }

    const rows = (data ?? []) as ExportRow[];
    const lines = [
      CSV_HEADER.map(csvCell).join(','),
      ...rows.map((r) =>
        [
          csvCell(r.email),
          csvCell(r.phone),
          csvCell(r.name),
          csvCell(formatDate(r.created_at)),
          csvCell(r.conversation_count ?? 0),
        ].join(','),
      ),
    ];
    // BOM para Excel reconhecer UTF-8 (acentos pt-BR).
    const csv = '﻿' + lines.join('\r\n') + '\r\n';

    return new Response(csv, {
      status: 200,
      headers: {
        'Content-Type': 'text/csv; charset=utf-8',
        'Content-Disposition': 'attachment; filename="contatos.csv"',
        'Cache-Control': 'no-store',
      },
    });
  } catch (err) {
    return apiError('Erro ao exportar contatos', {
      request,
      status: 500,
      cause: err,
      logMessage: '[contacts/export] unexpected error',
    });
  }
}
