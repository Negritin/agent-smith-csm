'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Contact, Copy, Download, MessageSquare, MoreVertical, Phone, Search } from 'lucide-react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import {
  PageActions,
  PageDescription,
  PageHeader,
  PageShell,
  PageTitle,
} from '@/components/ui/page-shell';
import { StatusPill } from '@/components/ui/status-pill';
import { FilterActions, FilterBar, FilterGroup, SearchField } from '@/components/ui/filter-bar';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Input } from '@/components/ui/input';
import { EmptyStatePanel, LoadingState } from '@/components/ui/feedback-state';

// ---- Types (mirror S1 rpc_list_contacts item shape) ----
interface ContactItem {
  user_id: string | null;
  name: string | null;
  phone: string | null;
  email: string | null;
  channel: string | null;
  created_at: string | null;
  last_seen: string | null;
  conversation_count: number;
}

const PAGE_SIZE = 25;
const ALL_CHANNELS = 'all';

function formatDate(value: string | null): string {
  if (!value) return '—';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit', year: 'numeric' });
}

export default function ContactsPage() {
  const router = useRouter();

  const [items, setItems] = useState<ContactItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);

  const [searchQuery, setSearchQuery] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [channel, setChannel] = useState<string>(ALL_CHANNELS);
  const [createdFrom, setCreatedFrom] = useState('');
  const [createdTo, setCreatedTo] = useState('');
  const [page, setPage] = useState(1);

  // 300ms debounce — copy of app/admin/conversations/page.tsx:166-170
  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(searchQuery.trim()), 300);
    return () => clearTimeout(t);
  }, [searchQuery]);

  // Reset to page 1 whenever a filter changes (so total/offset stay consistent)
  useEffect(() => {
    setPage(1);
  }, [debouncedSearch, channel, createdFrom, createdTo]);

  // Build the shared query string (used by both the list fetch and the export link)
  const buildParams = useCallback(
    (opts?: { withPaging?: boolean }) => {
      const params = new URLSearchParams();
      if (debouncedSearch) params.set('search', debouncedSearch);
      if (channel !== ALL_CHANNELS) params.set('channel', channel);
      if (createdFrom) params.set('created_from', createdFrom);
      if (createdTo) params.set('created_to', createdTo);
      if (opts?.withPaging) {
        params.set('page', String(page));
        params.set('page_size', String(PAGE_SIZE));
      }
      return params;
    },
    [debouncedSearch, channel, createdFrom, createdTo, page],
  );

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      setLoading(true);
      try {
        const params = buildParams({ withPaging: true });
        const res = await fetch(`/api/admin/contacts?${params.toString()}`);
        if (!res.ok) throw new Error('Falha ao carregar contatos');
        const data = await res.json();
        if (cancelled) return;
        setItems(Array.isArray(data.items) ? data.items : []);
        setTotal(typeof data.total === 'number' ? data.total : 0);
      } catch (err) {
        if (cancelled) return;
        console.error('Error loading contacts:', err);
        toast.error('Não foi possível carregar os contatos.');
        setItems([]);
        setTotal(0);
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    load();
    return () => {
      cancelled = true;
    };
  }, [buildParams]);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const handleViewConversations = (userId: string | null) => {
    if (!userId) {
      toast.error('Contato sem histórico vinculado.');
      return;
    }
    router.push(`/admin/conversations?contact_user_id=${encodeURIComponent(userId)}`);
  };

  const handleCopy = async (value: string | null, label: string) => {
    if (!value) {
      toast.error(`Sem ${label} para copiar.`);
      return;
    }
    try {
      await navigator.clipboard.writeText(value);
      toast.success(`${label} copiado.`);
    } catch {
      toast.error(`Não foi possível copiar o ${label}.`);
    }
  };

  // Server-side CSV export — NO client-side Blob from page rows (SPEC C1/D7)
  const handleExport = () => {
    if (total === 0) return;
    const params = buildParams(); // same filters, NO paging
    const url = `/api/admin/contacts/export?${params.toString()}`;
    const a = document.createElement('a');
    a.href = url;
    a.download = 'contatos.csv';
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast.success('Gerando CSV de contatos...');
  };

  const rangeLabel = useMemo(() => {
    if (total === 0) return '0 de 0';
    const start = (page - 1) * PAGE_SIZE + 1;
    const end = Math.min(page * PAGE_SIZE, total);
    return `${start} - ${end} de ${total}`;
  }, [page, total]);

  return (
    <PageShell>
      <PageHeader>
        <div>
          <PageTitle className="flex items-center gap-3">
            <Contact className="h-8 w-8 text-primary" />
            Contatos
          </PageTitle>
          <PageDescription>Gerencie seus contatos e leads</PageDescription>
        </div>
        <PageActions>
          <StatusPill tone="brand">{total} CONTATOS</StatusPill>
          <Button variant="outline" onClick={handleExport} disabled={total === 0}>
            <Download className="mr-2 h-4 w-4" />
            Exportar Contatos
          </Button>
        </PageActions>
      </PageHeader>

      <FilterBar>
        <FilterGroup>
          <SearchField
            icon={Search}
            placeholder="Buscar contatos (email, telefone, nome)"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
        </FilterGroup>
        <FilterActions>
          <Select value={channel} onValueChange={setChannel}>
            <SelectTrigger className="w-full sm:w-[180px]">
              <SelectValue placeholder="Canal" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_CHANNELS}>Todos os canais</SelectItem>
              <SelectItem value="whatsapp">WhatsApp</SelectItem>
              <SelectItem value="widget">Widget</SelectItem>
              <SelectItem value="web">Web</SelectItem>
            </SelectContent>
          </Select>
          <Input
            type="date"
            aria-label="Criado a partir de"
            value={createdFrom}
            onChange={(e) => setCreatedFrom(e.target.value)}
            className="w-full sm:w-[160px]"
          />
          <Input
            type="date"
            aria-label="Criado até"
            value={createdTo}
            onChange={(e) => setCreatedTo(e.target.value)}
            className="w-full sm:w-[160px]"
          />
        </FilterActions>
      </FilterBar>

      {loading ? (
        <LoadingState label="Carregando contatos..." />
      ) : items.length === 0 ? (
        <EmptyStatePanel
          icon={Contact}
          title="Nenhum contato encontrado"
          description="Ajuste os filtros ou aguarde novas conversas para popular esta lista."
        />
      ) : (
        <div className="overflow-hidden">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Email</TableHead>
                <TableHead>Número de Telefone</TableHead>
                <TableHead>Nome</TableHead>
                <TableHead>Criado em</TableHead>
                <TableHead className="w-[64px] text-right">Ações</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((c) => (
                <TableRow key={c.user_id ?? `${c.phone}-${c.email}-${c.created_at}`}>
                  <TableCell>{c.email ?? '—'}</TableCell>
                  <TableCell>{c.phone ?? '—'}</TableCell>
                  <TableCell>{c.name ?? '—'}</TableCell>
                  <TableCell>{formatDate(c.created_at)}</TableCell>
                  <TableCell className="text-right">
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button variant="ghost" size="icon">
                          <MoreVertical className="h-4 w-4" />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        <DropdownMenuItem onClick={() => handleViewConversations(c.user_id)}>
                          <MessageSquare className="mr-2 h-4 w-4" />
                          Ver conversas
                        </DropdownMenuItem>
                        <DropdownMenuItem onClick={() => handleCopy(c.email, 'email')}>
                          <Copy className="mr-2 h-4 w-4" />
                          Copiar email
                        </DropdownMenuItem>
                        <DropdownMenuItem onClick={() => handleCopy(c.phone, 'telefone')}>
                          <Phone className="mr-2 h-4 w-4" />
                          Copiar telefone
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>

          {/* hand-rolled pagination driven by total/PAGE_SIZE (house style) */}
          <div className="flex items-center justify-between border-t border-border px-6 py-4">
            <p className="text-sm text-muted-foreground">Mostrando {rangeLabel}</p>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page === 1}
              >
                Anterior
              </Button>
              <span className="px-2 text-sm text-muted-foreground">
                {page} / {totalPages}
              </span>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                disabled={page >= totalPages}
              >
                Próximo
              </Button>
            </div>
          </div>
        </div>
      )}
    </PageShell>
  );
}
