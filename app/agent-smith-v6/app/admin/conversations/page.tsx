'use client';

import { useEffect, useState, useRef, useMemo, Suspense } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import {
  MessageSquare,
  MessageCircle,
  Globe,
  Search,
  User,
  Filter,
  Send,
  MoreVertical,
  CheckCheck,
  RefreshCw,
  Code,
  X,
  Mic,
  Image as ImageIcon,
  Square,
  Loader2,
  Hand,
  PanelRightOpen,
  AlertTriangle,
  Clock,
  Timer,
  ArrowLeft,
  Building2,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import VoiceMessage from '@/components/VoiceMessage';
import { Input } from '@/components/ui/input';
import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  ChatComposerDock,
  ChatDetailsAside,
  ChatFrame,
  ChatMain,
  ChatTopbar,
  ChatViewport,
  ConversationRail,
} from '@/components/chat/chat-frame';
import { ConversationDetailsPanel, statusLabel } from '@/components/chat/ConversationDetailsPanel';
import { Sheet, SheetContent, SheetHeader, SheetTitle } from '@/components/ui/sheet';
import {
  useConversationDetailsPolling,
  useConversationListPolling,
  type ConversationListFilters,
} from '@/hooks/use-conversation-polling';
import { SlaIndicator } from '@/components/chat/SlaIndicator';
import { computeSlaProgress } from '@/lib/sla-progress';
import {
  pickListDeadline,
  quickFilterToServerFilters,
  slaBadgeKind,
  type QuickFilter,
} from '@/lib/conversation-list-filters';
import type {
  ChannelFilter as CanonicalChannelFilter,
  ConversationListItem,
  ConversationDetails,
} from '@/types/conversation-details';
import { LoadingState } from '@/components/ui/feedback-state';
import { QueuePicker, QUEUE_DEFS, type QueueId } from '@/components/chat/QueuePicker';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  DropdownMenuSeparator,
  DropdownMenuLabel,
} from '@/components/ui/dropdown-menu';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
// S9: o cliente Supabase anon é mantido APENAS para upload de mídia em buckets
// públicos (storage). A atualização ao vivo de lista/chat/card NÃO depende mais
// de Realtime anon — usa POLLING autenticado (iron-session) via
// `use-conversation-polling` e `/api/messages`. As subscriptions
// `supabase.channel('admin-inbox')` (conversations) e `admin-messages:<id>`
// (messages) foram REMOVIDAS aqui; isso é o pré-requisito para o S11 fechar a
// policy/grants `anon` no banco com segurança (SPEC §17 item 7, D2).
import { supabase } from '@/lib/supabase'; // KEPT: Only for storage uploads (public buckets)
import { useAdminRole } from '@/hooks/useAdminRole';
import { toast } from 'sonner';
import type { Company, Message } from '@/lib/types';
import {
  extractAllUCPData,
  ProductCarousel,
  ProductCard,
  CheckoutButton,
  UCPData,
} from '@/components/ucp';

// --- TIPOS ---
interface Conversation {
  id: string;
  user_name: string | null;
  user_phone: string | null;
  user_avatar: string | null;
  unread_count: number;
  last_message_preview: string | null;
  agent_name: string;
  agent_id: string | null;
  session_id: string; // 🔔 NOVO: para enviar mensagem
  status: string; // 🔔 NOVO: open, HUMAN_REQUESTED, closed
  created_at: string;
  last_message_at: string;
  status_color: 'red' | 'yellow' | 'green';
  channel: string;
  // S10 — campos enriquecidos da lista (GET /api/admin/conversations, S7).
  sla_health_status: ConversationListItem['sla_health_status'];
  sla_level: ConversationListItem['sla_level'];
  sla_first_response_deadline: string | null;
  sla_resolution_deadline: string | null;
  // S6 — âncoras da barra de progresso de SLA.
  sla_started_at: string | null;
  sla_first_response_at: string | null;
  has_active_timer: boolean;
  assigned_user_id: string | null;
  last_human_message_at: string | null;
  customer_waiting_since: string | null;
  users_v2?: {
    first_name: string | null;
    last_name: string | null;
    avatar_url: string | null;
    email: string | null;
  } | null;
  agents?: {
    id: string;
    name: string;
  } | null;
}

/**
 * F4 — Constrói um `Conversation` (shape de exibição do header/composer) a
 * partir do contrato `ConversationDetails` do `/details`, que SOBREVIVE ao
 * claim (vem de `useConversationDetailsPolling`, independente da lista
 * filtrada). Usado como fallback quando o item sai da lista ao virar
 * HUMAN_ACTIVE. Espelha o display-name + defaults de SLA do memo `conversations`
 * (L244-277) para manter o header idêntico.
 */
function mapDetailsToConversation(
  details: ConversationDetails | null | undefined,
): Conversation | undefined {
  const c = details?.conversation;
  if (!c) return undefined;
  const sla = details?.sla;
  const displayName = c.user_name || c.user_phone || c.user_email || 'Usuário Desconhecido';
  return {
    id: c.id,
    user_name: displayName,
    user_phone: c.user_phone,
    user_avatar: c.user_avatar,
    unread_count: c.unread_count ?? 0,
    last_message_preview: c.last_message_preview,
    agent_name: c.agent_name || 'Smith Agent',
    agent_id: c.agent_id,
    session_id: c.session_id ?? '',
    status: c.status || 'open',
    created_at: c.created_at ?? '',
    last_message_at: c.last_message_at ?? c.created_at ?? '',
    status_color: (c.status_color as 'red' | 'yellow' | 'green') || 'green',
    channel: c.channel ?? '',
    sla_health_status: sla?.health_status ?? 'none',
    sla_level: sla?.level ?? null,
    sla_first_response_deadline: sla?.first_response_deadline ?? null,
    sla_resolution_deadline: sla?.resolution_deadline ?? null,
    // S6 — campos de início de SLA (mesmos nomes do memo da lista).
    sla_started_at: details?.current_session?.started_at ?? null,
    sla_first_response_at: sla?.first_response_at ?? null,
    has_active_timer: !!details?.active_timer,
    assigned_user_id: c.assigned_user_id ?? null,
    last_human_message_at: c.last_human_message_at ?? null,
    customer_waiting_since: c.customer_waiting_since ?? null,
    users_v2: null,
    agents: c.agent_id ? { id: c.agent_id, name: c.agent_name || 'Smith Agent' } : null,
  };
}

// S10 — filtros canônicos (§12.3). Canal trata `widget` ALÉM de `web`.
type ChannelFilter = CanonicalChannelFilter; // all | whatsapp | widget | web
// QuickFilter é importado de lib/conversation-list-filters (lógica pura testável).

/**
 * Estados em que o atendimento humano está em curso e o composer deve ficar
 * habilitado (§6.3). Inclui os 3 estados humanos: `HUMAN_REQUESTED` (aguardando
 * responsável), `HUMAN_ACTIVE` (humano respondendo) e `PENDING_CUSTOMER` (humano
 * respondeu, aguardando cliente). ANTES da S6, só `HUMAN_REQUESTED` mantinha o
 * composer visível; como `record_human_message` agora avança o status para
 * `PENDING_CUSTOMER` no 1º envio, sem incluir os outros 2 estados o composer e o
 * botão "Devolver para IA" sumiriam após a 1ª mensagem (regressão multi-mensagem).
 * A UI definitiva (card lateral, S9/S10) substituirá este controle.
 */
const HUMAN_ATTENDANCE_STATUSES = ['HUMAN_REQUESTED', 'HUMAN_ACTIVE', 'PENDING_CUSTOMER'];

function isHumanAttendance(status: string | undefined | null): boolean {
  return status != null && HUMAN_ATTENDANCE_STATUSES.includes(status);
}

function AdminConversationsPageInner() {
  const { role, companyId, userId } = useAdminRole();
  const router = useRouter();
  const searchParams = useSearchParams();
  const isMaster = role === 'master';

  const [masterCompanies, setMasterCompanies] = useState<Company[]>([]);
  const [isLoadingCompanies, setIsLoadingCompanies] = useState(false);
  const [masterCompanyId, setMasterCompanyId] = useState<string | null>(() => {
    return searchParams.get('company_id') || searchParams.get('companyId');
  });

  // ============================================================
  // [NEW] Estados dos Filtros (§12.3)
  // ============================================================
  const [searchQuery, setSearchQuery] = useState('');
  const [channelFilter, setChannelFilter] = useState<ChannelFilter>('all');
  const [quickFilter, setQuickFilter] = useState<QuickFilter>('all');

  // Seletor de filas do inbox: `null` = mostra os cards das filas; um QueueId =
  // mostra a lista filtrada daquela fila (com seta de voltar). A fila define o
  // filtro de status primário enviado ao servidor (despoluído).
  const [selectedQueue, setSelectedQueue] = useState<QueueId | null>(null);
  const activeQueue = selectedQueue ? QUEUE_DEFS.find((q) => q.id === selectedQueue) : null;

  // Deep-link F1.5: id do CONTATO vindo da tela de Contatos (?contact_user_id=).
  // SEPARADO do `userId` do operador (useAdminRole) — nunca reusar aquele aqui.
  const [contactUserId, setContactUserId] = useState<string | null>(() =>
    searchParams.get('contact_user_id'),
  );

  const effectiveCompanyId = isMaster ? masterCompanyId : companyId;
  const companyScopedPath = (path: string): string => {
    if (!effectiveCompanyId) return path;
    return `${path}${path.includes('?') ? '&' : '?'}company_id=${encodeURIComponent(effectiveCompanyId)}`;
  };
  const conversationsBasePath = effectiveCompanyId
    ? `/admin/conversations?company_id=${encodeURIComponent(effectiveCompanyId)}`
    : '/admin/conversations';

  useEffect(() => {
    if (!isMaster) {
      setMasterCompanies([]);
      setMasterCompanyId(null);
      return;
    }

    let cancelled = false;
    setIsLoadingCompanies(true);
    const loadCompanies = async () => {
      try {
        const res = await fetch('/api/admin/companies?status=all', { credentials: 'include' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const companies = Array.isArray(data.companies) ? (data.companies as Company[]) : [];
        if (cancelled) return;
        setMasterCompanies(companies);
        setMasterCompanyId((current) => {
          if (current && companies.some((company) => company.id === current)) return current;
          return companies[0]?.id ?? null;
        });
      } catch {
        if (!cancelled) {
          setMasterCompanies([]);
          toast.error('Erro ao carregar empresas.');
        }
      } finally {
        if (!cancelled) setIsLoadingCompanies(false);
      }
    };

    void loadCompanies();
    return () => {
      cancelled = true;
    };
  }, [isMaster]);

  useEffect(() => {
    if (!isMaster || !masterCompanyId) return;
    const params = new URLSearchParams(searchParams.toString());
    if (params.get('company_id') === masterCompanyId && !params.has('companyId')) return;
    params.set('company_id', masterCompanyId);
    params.delete('companyId');
    router.replace(`/admin/conversations?${params.toString()}`);
  }, [isMaster, masterCompanyId, router, searchParams]);

  // Debounce da busca para o servidor (evita 1 request por tecla; a UI segue
  // responsiva via `searchQuery` enquanto o request usa `debouncedSearch`).
  const [debouncedSearch, setDebouncedSearch] = useState('');
  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(searchQuery.trim()), 300);
    return () => clearTimeout(t);
  }, [searchQuery]);

  // Mapeia o atalho rápido (§12.3) para os filtros canônicos do servidor (S7).
  // Lógica PURA extraída em lib/conversation-list-filters (testável).
  const serverFilters: ConversationListFilters = useMemo(() => {
    const f = quickFilterToServerFilters(quickFilter, {
      channel: channelFilter,
      search: debouncedSearch,
      userId,
    });
    // A fila selecionada É o filtro de status primário (sobrepõe o status que o
    // atalho rápido por acaso tenha definido). Os demais campos (canal/busca/SLA)
    // continuam compondo normalmente.
    if (activeQueue) f.status = activeQueue.serverStatus;
    // Deep-link de contato: filtra por user_id SEM forçar status (validação C4) —
    // sem fila ativa, activeQueue é null, então f.status fica indefinido e o
    // contato aparece em TODAS as filas.
    if (contactUserId) f.contact_user_id = contactUserId;
    if (effectiveCompanyId) f.company_id = effectiveCompanyId;
    return f;
  }, [
    channelFilter,
    debouncedSearch,
    quickFilter,
    userId,
    activeQueue,
    contactUserId,
    effectiveCompanyId,
  ]);

  // ============================================================
  // S9/S10 — POLLING AUTENTICADO da lista (substitui a subscription Supabase
  // Realtime ANÔNIMA 'admin-inbox'). Fonte de atualização da lista via
  // `GET /api/admin/conversations` (iron-session), agora com os FILTROS
  // CANÔNICOS aplicados server-side (§12.3) e a priorização §6.1 vinda do
  // backend (S7). Mantém local overlay (unread_count) por cima do snapshot.
  // ============================================================
  const {
    conversations: rawConversations,
    isLoading: isLoadingList,
    refetch: refetchList,
  } = useConversationListPolling({
    enabled: !!effectiveCompanyId && (selectedQueue !== null || !!contactUserId),
    filters: serverFilters,
  });

  // Contadores por fila (seletor do inbox). Endpoint leve de head-counts; refaz
  // ao entrar/sair de uma fila (selectedQueue muda) e a cada 15s.
  const [queueCounts, setQueueCounts] = useState<Partial<Record<QueueId, number>>>({});
  const [queueTotal, setQueueTotal] = useState<number | null>(null);
  useEffect(() => {
    if (!effectiveCompanyId) return;
    let cancelled = false;
    const loadCounts = async () => {
      try {
        const res = await fetch(
          `/api/admin/conversations/counts?company_id=${encodeURIComponent(effectiveCompanyId)}`,
          { credentials: 'include' },
        );
        if (!res.ok || cancelled) return;
        const data = await res.json();
        if (cancelled) return;
        const byQueue = {
          agente: data.agente ?? 0,
          humano: data.humano ?? 0,
          nao_respondido: data.nao_respondido ?? 0,
          finalizado: data.finalizado ?? 0,
        };
        setQueueCounts(byQueue);
        // "X conversas no total" = soma das 4 filas (mesmo conjunto dos cards),
        // garantindo que o rodapé sempre bata com os números exibidos.
        setQueueTotal(
          byQueue.agente + byQueue.humano + byQueue.nao_respondido + byQueue.finalizado,
        );
      } catch {
        // Best-effort: contador é informativo, não bloqueia o uso.
      }
    };
    loadCounts();
    const t = setInterval(loadCounts, 15000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [effectiveCompanyId, selectedQueue]);

  // Overlay local de unread (zerar ao abrir a conversa sem esperar o próximo poll).
  const [readOverlay, setReadOverlay] = useState<Record<string, number>>({});

  // Snapshot mapeado para o shape de exibição (mantém compat com o JSX atual).
  const conversations: Conversation[] = useMemo(() => {
    return (rawConversations as any[]).map((conv) => {
      const profileName = conv.users_v2?.first_name
        ? `${conv.users_v2.first_name} ${conv.users_v2.last_name || ''}`.trim()
        : null;
      const displayName =
        conv.user_name ||
        profileName ||
        conv.user_phone ||
        conv.users_v2?.email ||
        conv.user_email ||
        'Usuário Desconhecido';
      const displayAvatar = conv.user_avatar || conv.users_v2?.avatar_url;
      return {
        ...conv,
        user_name: displayName,
        user_avatar: displayAvatar,
        agent_name: conv.agents?.name || conv.agent_name || 'Smith Agent',
        status_color: conv.status_color || 'green',
        status: conv.status || 'open',
        // S10 — campos enriquecidos (S7); defaults seguros p/ conversas antigas.
        sla_health_status: conv.sla_health_status || 'none',
        sla_level: conv.sla_level ?? null,
        sla_first_response_deadline: conv.sla_first_response_deadline ?? null,
        sla_resolution_deadline: conv.sla_resolution_deadline ?? null,
        // S6 — âncoras da barra de SLA (default null p/ conversas sem sessão).
        sla_started_at: conv.sla_started_at ?? null,
        sla_first_response_at: conv.sla_first_response_at ?? null,
        has_active_timer: !!conv.has_active_timer,
        assigned_user_id: conv.assigned_user_id ?? null,
        last_human_message_at: conv.last_human_message_at ?? null,
        customer_waiting_since: conv.customer_waiting_since ?? null,
        unread_count:
          readOverlay[conv.id] !== undefined ? readOverlay[conv.id] : (conv.unread_count ?? 0),
      } as Conversation;
    });
  }, [rawConversations, readOverlay]);

  // Estados do Chat Ativo
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [isLoadingChat, setIsLoadingChat] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setSelectedQueue(null);
    setSelectedId(null);
    setMessages([]);
    setQueueCounts({});
    setQueueTotal(null);
  }, [effectiveCompanyId]);

  // S9 — drawer do card lateral em telas < 1280px.
  const [detailsDrawerOpen, setDetailsDrawerOpen] = useState(false);

  // S9 — POLLING AUTENTICADO do card lateral (/details) da conversa aberta.
  const {
    details,
    isLoading: isLoadingDetails,
    error: detailsError,
    refetch: refetchDetails,
  } = useConversationDetailsPolling({ conversationId: selectedId, companyId: effectiveCompanyId });

  // 🔔 Estado para resposta humana
  const [humanReplyText, setHumanReplyText] = useState('');
  const [isSendingReply, setIsSendingReply] = useState(false);
  const [adminName, setAdminName] = useState<string>('Admin');
  const [adminAvatar, setAdminAvatar] = useState<string | null>(null);

  // 🎤 Estados para upload de mídia
  const [isRecording, setIsRecording] = useState(false);
  const [isUploadingMedia, setIsUploadingMedia] = useState(false);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const audioChunksRef = useRef<Blob[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const humanReplyInputRef = useRef<HTMLInputElement>(null);

  // 🔔 Buscar nome e avatar do admin logado
  useEffect(() => {
    const fetchAdminData = async () => {
      try {
        const res = await fetch('/api/auth/me', { credentials: 'include' });
        if (res.ok) {
          const data = await res.json();
          if (data.user?.first_name) {
            setAdminName(`${data.user.first_name} ${data.user.last_name || ''}`.trim());
          } else if (data.user?.email) {
            setAdminName(data.user.email.split('@')[0]);
          }
          if (data.user?.avatar_url) {
            setAdminAvatar(data.user.avatar_url);
          }
        }
      } catch (err) {
        // Silent fail - not critical
      }
    };
    fetchAdminData();
  }, []);

  // ============================================================
  // [S10] Lista filtrada (§12.3).
  //
  // Os filtros canônicos (canal/status/SLA/busca) e a priorização §6.1 são
  // aplicados SERVER-SIDE (S7) — a UI apenas REFLETE a ordem do backend, sem
  // reordenar. Aqui só refinamos o atalho "Aguardando cliente" (PENDING_CUSTOMER),
  // que no servidor está agrupado dentro de `human`.
  //
  // PREMISSA (sem paginação no cliente): `useConversationListPolling`/`buildListQuery`
  // NÃO enviam `page`/`page_size`, então o backend opera em MODO LEGADO e devolve
  // TODA a janela varrida (MAX_SCAN=1000) — não uma página de `human`. Logo o
  // refino client-side de PENDING_CUSTOMER NÃO perde itens por corte de página
  // (route.ts: `hasPaginationParams` ⇒ pageSize=MAX_SCAN). Se um dia a UI passar a
  // paginar, expor `status=pending_customer` no servidor (S7) em vez de refinar aqui.
  // ============================================================
  const filteredConversations = useMemo(() => {
    if (quickFilter === 'pending_customer') {
      return conversations.filter((c) => c.status === 'PENDING_CUSTOMER');
    }
    return conversations;
  }, [conversations, quickFilter]);

  const hasActiveFilter =
    channelFilter !== 'all' || quickFilter !== 'all' || searchQuery.trim().length > 0;

  // S9: refetch manual da lista (botão "atualizar") agora delega ao hook de
  // polling autenticado. Mantido o nome `fetchConversations` para os callers.
  const fetchConversations = () => refetchList();

  // S9 — A "SETUP INICIAL E REALTIME" foi REMOVIDA: a lista atualiza via
  // `useConversationListPolling` (polling autenticado iron-session), sem a
  // subscription Supabase Realtime ANÔNIMA 'admin-inbox' que existia aqui. Esta é
  // a remoção de DEPENDÊNCIA do `anon` que destrava o S11 (SPEC §17 item 7, D2).

  // 2. FETCH + POLLING DE MENSAGENS (S9)
  //
  // Substitui a subscription Supabase Realtime ANÔNIMA `admin-messages:<id>` que
  // recebia INSERTs em `messages`. Agora as mensagens da conversa aberta são
  // buscadas via `GET /api/messages` (mesma origem autenticada) num intervalo
  // curto, com PAUSA quando a aba está inativa e cancelamento ao trocar de
  // conversa. Isto remove a última dependência do front em relação ao realtime
  // `anon` em `messages` — pré-requisito para o REVOKE de S11.
  useEffect(() => {
    if (!selectedId) {
      setMessages([]);
      return;
    }

    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    // Backoff: sucesso reseta para a base (resposta rápida do atendente); erro
    // contínuo (ex.: backend caído / 500 em /api/messages) recua até o teto, em
    // vez de martelar a cada 4s indefinidamente (§S9: backoff em erro).
    const MESSAGES_BASE_MS = 4000;
    const MESSAGES_MAX_MS = 30000;
    let delay = MESSAGES_BASE_MS;

    const loadMessages = async (initial: boolean) => {
      if (initial) setIsLoadingChat(true);
      try {
        const response = await fetch(`/api/messages?conversation_id=${selectedId}&scope=admin`, {
          credentials: 'include',
        });
        if (!response.ok) throw new Error('Falha ao buscar mensagens');
        const result = await response.json();
        if (cancelled) return;
        setMessages(result.messages || []);
        delay = MESSAGES_BASE_MS;

        if (initial) {
          // Marcar como lida via API + overlay local imediato.
          setReadOverlay((prev) => ({ ...prev, [selectedId]: 0 }));
          await fetch(`/api/conversations/${selectedId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
            body: JSON.stringify({ unread_count: 0 }),
          });
        }
      } catch (err) {
        delay = Math.min(MESSAGES_MAX_MS, delay * 2);
        if (!cancelled && initial) {
          console.error('Erro ao buscar mensagens:', err);
          toast.error('Erro ao carregar mensagens.');
        }
      } finally {
        if (!cancelled && initial) setIsLoadingChat(false);
      }
    };

    const schedule = () => {
      if (cancelled) return;
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
      timer = setTimeout(async () => {
        await loadMessages(false);
        schedule();
      }, delay);
    };

    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        if (timer) clearTimeout(timer);
        delay = MESSAGES_BASE_MS;
        void loadMessages(false);
        schedule();
      } else if (timer) {
        clearTimeout(timer);
        timer = null;
      }
    };

    void loadMessages(true).then(() => schedule());
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [selectedId]);

  // Auto-scroll para o final quando mensagens mudam ou chat é aberto
  useEffect(() => {
    if (scrollRef.current) {
      // setTimeout garante que o scroll aconteça após o render do DOM
      setTimeout(() => {
        scrollRef.current!.scrollTop = scrollRef.current!.scrollHeight;
      }, 0);
    }
  }, [messages, selectedId, isLoadingChat]); // Adicionado selectedId e loading

  // --- HELPERS ---
  const getBarColor = (color: string) => {
    switch (color) {
      case 'red':
        return 'bg-danger';
      case 'yellow':
        return 'bg-warning';
      case 'green':
        return 'bg-success';
      default:
        return 'bg-muted-foreground';
    }
  };

  const getTimeAgo = (dateString: string) => {
    if (!dateString) return '';
    const date = new Date(dateString);
    const now = new Date();
    const diffInHours = Math.abs(now.getTime() - date.getTime()) / 36e5;
    if (diffInHours < 1) return 'agora';
    if (diffInHours < 24) return `há ${Math.floor(diffInHours)}h`;
    return `há ${Math.floor(diffInHours / 24)}d`;
  };

  const getChannelBadge = (channel: string) => {
    const isWhatsapp = channel === 'whatsapp';
    const isWidget = channel === 'widget';

    let colorClass = '';
    let label = '';

    if (isWhatsapp) {
      colorClass = 'bg-success text-success-foreground border-success hover:bg-success/90';
      label = 'WhatsApp';
    } else if (isWidget) {
      colorClass = 'bg-primary text-primary-foreground border-primary hover:bg-primary/90';
      label = 'Widget';
    } else {
      colorClass = 'bg-primary text-primary-foreground border-primary hover:bg-primary/90';
      label = 'Web';
    }

    return (
      <Badge
        className={`h-5 px-1.5 text-[9px] uppercase tracking-wide border font-bold ${colorClass}`}
      >
        {label}
      </Badge>
    );
  };

  // ============================================================
  // [NEW] Helper para label do filtro ativo
  // ============================================================
  const getFilterLabel = () => {
    switch (channelFilter) {
      case 'whatsapp':
        return 'WhatsApp';
      case 'widget':
        return 'Widget';
      case 'web':
        return 'Web';
      default:
        return null;
    }
  };

  const QUICK_FILTER_LABELS: Record<QuickFilter, string> = {
    all: 'Todos',
    human: 'Humano',
    mine: 'Meus atendimentos',
    breached: 'SLA vencido',
    at_risk: 'SLA em risco',
    critical: 'SLA crítico',
    no_sla: 'Sem SLA',
    pending_customer: 'Aguardando cliente',
    resolved: 'Resolvidos',
  };

  // ============================================================
  // 🔔 Enviar resposta humana
  // ============================================================
  const handleSendHumanReply = async () => {
    // F4 — fallback no `details` (sobrevive ao claim) p/ não dar early-return
    // quando a conversa assumida saiu da lista filtrada.
    const conversation =
      conversations.find((c) => c.id === selectedId) ??
      (details?.conversation?.id === selectedId ? mapDetailsToConversation(details) : undefined);
    const text = humanReplyText.trim();
    if (!text || !selectedId || !conversation) return;

    // Estilo WhatsApp: limpa o campo na hora e devolve o foco pro input (caso o
    // clique no botão de enviar tenha roubado o cursor). O input NÃO é desabilitado
    // durante o envio, então o cursor não "salta" pra fora e dá pra digitar a próxima.
    setHumanReplyText('');
    humanReplyInputRef.current?.focus();
    setIsSendingReply(true);
    try {
      // 1. Salvar mensagem via API (bypassa RLS, usa sender_user_id via sessão)
      const response = await fetch(companyScopedPath('/api/admin/conversations/messages'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          conversation_id: selectedId,
          content: text, // Sem prefixo - backend usa sender_user_id
        }),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || 'Failed to send message');
      }

      // WhatsApp delivery is now handled by the API Route server-side
      // No direct BACKEND_URL call needed here

      // S9: sem realtime anon — refetch imediato das mensagens e do card/lista
      // (o status pode avançar para PENDING_CUSTOMER no 1º envio humano).
      // Sem toast de "Mensagem enviada!" — o próprio balão na conversa já é o feedback.
      await refreshActiveMessages();
      refetchDetails();
      refetchList();
    } catch (err) {
      console.error('Erro ao enviar resposta:', err);
      toast.error('Erro ao enviar mensagem.');
      setHumanReplyText(text); // restaura o texto pra permitir reenvio
    } finally {
      setIsSendingReply(false);
      humanReplyInputRef.current?.focus();
    }
  };

  // S9: refetch das mensagens da conversa ATIVA sob demanda (após enviar/ação),
  // substituindo o push do realtime anon. Best-effort.
  const refreshActiveMessages = async () => {
    if (!selectedId) return;
    try {
      const response = await fetch(`/api/messages?conversation_id=${selectedId}&scope=admin`, {
        credentials: 'include',
      });
      if (!response.ok) return;
      const result = await response.json();
      setMessages(result.messages || []);
    } catch {
      // best-effort
    }
  };

  // ============================================================
  // 🔔 Encerrar atendimento humano (devolver para IA) — usa rota de AÇÃO do S6
  // (return-to-ai), NÃO o update direto de status.
  // ============================================================
  const handleCloseHumanHandoff = async () => {
    if (!selectedId) return;

    try {
      const response = await fetch(
        companyScopedPath(`/api/admin/conversations/${selectedId}/return-to-ai`),
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include',
          body: JSON.stringify({}),
        },
      );

      if (!response.ok) throw new Error('Failed to return to AI');

      toast.success('Atendimento devolvido para a IA!');
      refetchDetails();
      refetchList();
    } catch (err) {
      console.error('Erro ao encerrar handoff:', err);
      toast.error('Erro ao devolver para IA.');
    }
  };

  // ============================================================
  // 🖐️ Assumir Conversa (Take Over)
  // ============================================================
  const handleTakeOver = async () => {
    if (!selectedId) return;

    try {
      // TOMADA MANUAL = claim (§3.2/§11.1: NÃO dispara alerta de handoff). Usa a
      // rota dedicada POST [id]/claim, que vai a HUMAN_ACTIVE em uma transação SEM
      // enfileirar notification_deliveries. (Antes este botão enviava
      // status='HUMAN_REQUESTED' ao shim PUT /status, que mapeia para
      // request_handoff e NOTIFICA os destinatários — efeito espúrio contrário à
      // intenção de "assumir"; corrigido aqui ao adiantar a migração do caller.)
      const response = await fetch(
        companyScopedPath(`/api/admin/conversations/${selectedId}/claim`),
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ reason: 'Intervenção Manual do Admin' }),
        },
      );

      if (!response.ok) throw new Error('Failed to take over');

      // F4 — auto-troca p/ a fila Humano (mantém `selectedId`) para o item
      // reaparecer na lista filtrada após o claim -> HUMAN_ACTIVE. Defesa em
      // profundidade junto ao fallback no `details` (activeConversation).
      setSelectedQueue('humano');
      toast.success('Conversa assumida — movida para Atendimento Humano. A IA foi pausada.');
      refetchDetails();
      refetchList();
    } catch (err) {
      console.error('Erro ao assumir conversa:', err);
      toast.error('Erro ao assumir conversa.');
    }
  };

  // ============================================================
  // 🖼️ Upload de Imagem
  // ============================================================
  const handleImageSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file || !selectedId) return;

    setIsUploadingMedia(true);
    try {
      const timestamp = Date.now();
      const filename = `${timestamp}_${file.name}`;
      const path = `admin/${selectedId}/${filename}`;

      // Storage upload (still uses anon client, OK for public buckets)
      const { error: uploadError } = await supabase.storage.from('chat-media').upload(path, file);

      if (uploadError) throw uploadError;

      const {
        data: { publicUrl },
      } = supabase.storage.from('chat-media').getPublicUrl(path);

      // Inserir mensagem via API (bypassa RLS)
      const response = await fetch(companyScopedPath('/api/admin/conversations/messages'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          conversation_id: selectedId,
          content: '📷 Imagem enviada', // Sem prefixo - backend usa sender_user_id
          image_url: publicUrl,
          type: 'text',
        }),
      });

      if (!response.ok) throw new Error('Failed to save message');

      // WhatsApp delivery is now handled by the API Route server-side

      toast.success('Imagem enviada!');
      await refreshActiveMessages(); // S9: sem realtime anon
    } catch (err) {
      console.error('Erro no upload de imagem:', err);
      toast.error('Erro ao enviar imagem.');
    } finally {
      setIsUploadingMedia(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  // ============================================================
  // 🎤 Gravação de Áudio
  // ============================================================
  const startRecording = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mediaRecorder = new MediaRecorder(stream);
      mediaRecorderRef.current = mediaRecorder;
      audioChunksRef.current = [];

      mediaRecorder.ondataavailable = (e) => {
        audioChunksRef.current.push(e.data);
      };

      mediaRecorder.onstop = async () => {
        const audioBlob = new Blob(audioChunksRef.current, { type: 'audio/webm' });
        stream.getTracks().forEach((track) => track.stop());
        await sendAudioMessage(audioBlob);
      };

      mediaRecorder.start();
      setIsRecording(true);
    } catch (err) {
      console.error('Erro ao iniciar gravação:', err);
      toast.error('Erro ao acessar microfone.');
    }
  };

  const stopRecording = () => {
    if (mediaRecorderRef.current && isRecording) {
      mediaRecorderRef.current.stop();
      setIsRecording(false);
    }
  };

  const sendAudioMessage = async (audioBlob: Blob) => {
    if (!selectedId) return;

    setIsUploadingMedia(true);
    try {
      const timestamp = Date.now();
      const filename = `${timestamp}_audio.webm`;
      const path = `admin/${selectedId}/${filename}`;

      // Storage upload (still uses anon client, OK for public buckets)
      const { error: uploadError } = await supabase.storage
        .from('voice-messages')
        .upload(path, audioBlob);

      if (uploadError) throw uploadError;

      const {
        data: { publicUrl },
      } = supabase.storage.from('voice-messages').getPublicUrl(path);

      // Inserir mensagem via API (bypassa RLS)
      const response = await fetch(companyScopedPath('/api/admin/conversations/messages'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          conversation_id: selectedId,
          content: '🎤 Áudio enviado', // Sem prefixo - backend usa sender_user_id
          audio_url: publicUrl,
          type: 'voice',
        }),
      });

      if (!response.ok) throw new Error('Failed to save message');

      // WhatsApp delivery is now handled by the API Route server-side

      toast.success('Áudio enviado!');
      await refreshActiveMessages(); // S9: sem realtime anon
    } catch (err) {
      console.error('Erro no upload de áudio:', err);
      toast.error('Erro ao enviar áudio.');
    } finally {
      setIsUploadingMedia(false);
    }
  };

  // F4 — header do chat desacoplado da lista filtrada: se o item saiu da lista
  // (ex.: virou HUMAN_ACTIVE após Assumir), cai no snapshot do `/details`.
  const activeConversation =
    conversations.find((c) => c.id === selectedId) ??
    (details?.conversation?.id === selectedId ? mapDetailsToConversation(details) : undefined);

  // Render gate (validação B-render): mostrar a lista quando há fila OU deep-link de contato.
  const showConversationList = selectedQueue !== null || !!contactUserId;

  return (
    <ChatFrame className="h-full">
      {/* ================= SIDEBAR (LISTA) ================= */}
      <ConversationRail>
        {isMaster && (
          <div className="shrink-0 border-b border-border p-3">
            <div className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              <Building2 className="h-3.5 w-3.5" />
              Empresa
            </div>
            <Select
              value={masterCompanyId ?? undefined}
              onValueChange={(value) => {
                setMasterCompanyId(value);
                setSelectedQueue(null);
                setSelectedId(null);
                setMessages([]);
                setContactUserId(null);
                setQueueCounts({});
                setQueueTotal(null);
                const params = new URLSearchParams(searchParams.toString());
                params.set('company_id', value);
                params.delete('companyId');
                params.delete('contact_user_id');
                router.replace(`/admin/conversations?${params.toString()}`);
              }}
              disabled={isLoadingCompanies || masterCompanies.length === 0}
            >
              <SelectTrigger className="h-9 bg-muted/40">
                <SelectValue
                  placeholder={isLoadingCompanies ? 'Carregando empresas...' : 'Selecionar empresa'}
                />
              </SelectTrigger>
              <SelectContent>
                {masterCompanies.map((company) => (
                  <SelectItem key={company.id} value={company.id}>
                    {company.company_name || company.legal_name || company.id}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}

        {isMaster && !effectiveCompanyId ? (
          <div className="flex min-h-0 flex-1 items-center justify-center p-8 text-center text-sm text-muted-foreground">
            Selecione uma empresa para abrir a inbox de atendimento.
          </div>
        ) : !showConversationList ? (
          <QueuePicker
            counts={queueCounts}
            total={queueTotal}
            isLoading={queueTotal === null}
            onSelect={(id) => {
              setSelectedQueue(id);
              setSelectedId(null);
            }}
          />
        ) : (
          <>
            {/* Header Sidebar — cabeçalho da fila (voltar + título + contador) */}
            <div className="p-4 border-b border-border flex-shrink-0">
              <div className="flex items-center justify-between mb-3">
                <div className="flex min-w-0 items-center gap-2">
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7 shrink-0 text-muted-foreground hover:text-foreground"
                    onClick={() => {
                      setSelectedQueue(null);
                      setSelectedId(null);
                      // Deep-link: limpar o estado E o param da URL, senão
                      // showConversationList continua true e o usuário fica preso
                      // na lista (validação B-render); um refresh re-prenderia.
                      setContactUserId(null);
                      router.replace(conversationsBasePath);
                    }}
                    title="Voltar às filas"
                    aria-label="Voltar às filas"
                  >
                    <ArrowLeft className="h-4 w-4" />
                  </Button>
                  <div className="min-w-0">
                    <h2 className="truncate text-base font-bold text-foreground">
                      {activeQueue?.title ?? 'Conversas'}
                    </h2>
                    <p className="text-xs text-muted-foreground">
                      {filteredConversations.length} conversa
                      {filteredConversations.length === 1 ? '' : 's'}
                    </p>
                  </div>
                  {/* Badge mostrando filtro de canal ativo */}
                  {channelFilter !== 'all' && (
                    <Badge
                      variant="secondary"
                      className="text-[10px] bg-accent/20 text-accent border-accent/25 cursor-pointer hover:bg-accent/30"
                      onClick={() => setChannelFilter('all')}
                    >
                      {getFilterLabel()}
                      <X className="w-3 h-3 ml-1" />
                    </Badge>
                  )}
                  {/* Badge mostrando filtro rápido ativo (§12.3) */}
                  {quickFilter !== 'all' && (
                    <Badge
                      variant="secondary"
                      className="text-[10px] bg-primary/15 text-primary border-primary/25 cursor-pointer hover:bg-primary/25"
                      onClick={() => setQuickFilter('all')}
                    >
                      {QUICK_FILTER_LABELS[quickFilter]}
                      <X className="w-3 h-3 ml-1" />
                    </Badge>
                  )}
                </div>
                <div className="flex gap-2">
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-6 w-6 text-muted-foreground hover:text-foreground"
                    onClick={() => fetchConversations()}
                    title="Atualizar lista"
                  >
                    <RefreshCw className="h-4 w-4" />
                  </Button>

                  {/* ============================================================ */}
                  {/* [NEW] Dropdown de Filtro por Canal */}
                  {/* ============================================================ */}
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button
                        variant="ghost"
                        size="icon"
                        className={`h-6 w-6 hover:text-foreground ${channelFilter !== 'all' ? 'text-accent' : 'text-muted-foreground'}`}
                        title="Filtrar por canal"
                      >
                        <Filter className="h-4 w-4" />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent
                      align="end"
                      className="bg-card border-border text-foreground min-w-[160px]"
                    >
                      <DropdownMenuLabel className="text-muted-foreground text-xs">
                        Filtrar por Canal
                      </DropdownMenuLabel>
                      <DropdownMenuSeparator className="bg-border" />

                      <DropdownMenuItem
                        onClick={() => setChannelFilter('all')}
                        className={`cursor-pointer ${channelFilter === 'all' ? 'bg-accent/20 text-accent' : 'hover:bg-muted'}`}
                      >
                        <MessageSquare className="w-4 h-4 mr-2" />
                        Todos os Canais
                      </DropdownMenuItem>

                      <DropdownMenuItem
                        onClick={() => setChannelFilter('whatsapp')}
                        className={`cursor-pointer ${channelFilter === 'whatsapp' ? 'bg-success/20 text-success' : 'hover:bg-muted'}`}
                      >
                        <div className="w-4 h-4 mr-2 rounded-full bg-success/10 flex items-center justify-center">
                          <MessageCircle className="w-2.5 h-2.5" />
                        </div>
                        WhatsApp
                      </DropdownMenuItem>

                      <DropdownMenuItem
                        onClick={() => setChannelFilter('widget')}
                        className={`cursor-pointer ${channelFilter === 'widget' ? 'bg-primary/15 text-primary' : 'hover:bg-muted'}`}
                      >
                        <div className="w-4 h-4 mr-2 rounded-full bg-primary/10 flex items-center justify-center">
                          <Code className="w-2.5 h-2.5" />
                        </div>
                        Widget
                      </DropdownMenuItem>

                      <DropdownMenuItem
                        onClick={() => setChannelFilter('web')}
                        className={`cursor-pointer ${channelFilter === 'web' ? 'bg-warning/15 text-warning' : 'hover:bg-muted'}`}
                      >
                        <div className="w-4 h-4 mr-2 rounded-full bg-warning/10 flex items-center justify-center">
                          <Globe className="w-2.5 h-2.5" />
                        </div>
                        Web Chat
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>

                  {/* ============================================================ */}
                  {/* [S10] Dropdown de Filtro rápido (Humano/SLA/etc — §12.3) */}
                  {/* ============================================================ */}
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button
                        variant="ghost"
                        size="icon"
                        className={`h-6 w-6 hover:text-foreground ${quickFilter !== 'all' ? 'text-primary' : 'text-muted-foreground'}`}
                        title="Filtros de atendimento"
                      >
                        <AlertTriangle className="h-4 w-4" />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent
                      align="end"
                      className="bg-card border-border text-foreground min-w-[180px]"
                    >
                      <DropdownMenuLabel className="text-muted-foreground text-xs">
                        Filtrar atendimento
                      </DropdownMenuLabel>
                      <DropdownMenuSeparator className="bg-border" />
                      {(Object.keys(QUICK_FILTER_LABELS) as QuickFilter[]).map((qf) => (
                        <DropdownMenuItem
                          key={qf}
                          onClick={() => setQuickFilter(qf)}
                          className={`cursor-pointer ${quickFilter === qf ? 'bg-primary/15 text-primary' : 'hover:bg-muted'}`}
                        >
                          {QUICK_FILTER_LABELS[qf]}
                        </DropdownMenuItem>
                      ))}
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>
              </div>

              {/* ============================================================ */}
              {/* [NEW] Input de Busca Funcional */}
              {/* ============================================================ */}
              <div className="relative">
                <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 text-muted-foreground h-4 w-4" />
                <Input
                  placeholder="Buscar por nome, telefone..."
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="pl-9 pr-9 bg-muted/50 border-border text-foreground focus:ring-primary h-9 text-sm placeholder:text-muted-foreground"
                />
                {searchQuery && (
                  <button
                    onClick={() => setSearchQuery('')}
                    className="absolute right-3 top-1/2 transform -translate-y-1/2 text-muted-foreground hover:text-foreground"
                  >
                    <X className="h-4 w-4" />
                  </button>
                )}
              </div>

              {/* Contador de resultados quando filtrando */}
              {hasActiveFilter && (
                <div className="mt-2 text-xs text-muted-foreground">
                  {filteredConversations.length} conversa(s) com esses filtros
                </div>
              )}
            </div>

            {/* Lista Scrollável — div simples (o ScrollArea do Radix força display:table
            no filho do viewport e estoura a largura da coluna, escondendo horário/badges). */}
            <div className="min-h-0 flex-1 overflow-y-auto">
              <div className="flex flex-col gap-2 p-2">
                {filteredConversations.length === 0 && !isLoadingList && (
                  <div className="p-8 text-center text-muted-foreground text-sm">
                    {hasActiveFilter
                      ? 'Nenhuma conversa encontrada com esses filtros.'
                      : 'Nenhuma conversa encontrada.'}
                  </div>
                )}

                {/* [CHANGED] Usa filteredConversations ao invés de conversations */}
                {filteredConversations.map((conv) => (
                  <div
                    key={conv.id}
                    onClick={() => setSelectedId(conv.id)}
                    className={`group relative flex cursor-pointer gap-3 rounded-xl border p-4 transition-[transform,box-shadow,background-color,border-color] duration-200 ease-[cubic-bezier(.16,1,.3,1)] hover:-translate-y-0.5 hover:bg-muted/40 hover:shadow-[var(--shadow-raised)] active:translate-y-0 ${
                      selectedId === conv.id
                        ? '-translate-y-0.5 border-primary/40 bg-muted shadow-[var(--shadow-raised)] ring-1 ring-primary/30'
                        : 'border-border bg-card'
                    }`}
                  >
                    <Avatar className="h-11 w-11 shrink-0 border border-border">
                      <AvatarImage src={conv.user_avatar || undefined} />
                      <AvatarFallback className="bg-primary text-primary-foreground font-bold text-xs">
                        {(conv.user_name || 'U').substring(0, 2).toUpperCase()}
                      </AvatarFallback>
                    </Avatar>

                    <div className="min-w-0 flex-1">
                      {/* Linha 1: nome + horário (estilo WhatsApp) */}
                      <div className="flex items-baseline gap-2">
                        <h3 className="min-w-0 flex-1 truncate text-[15px] font-semibold text-foreground">
                          {conv.user_name}
                        </h3>
                        <span className="shrink-0 text-[10px] font-medium text-muted-foreground">
                          {getTimeAgo(conv.last_message_at)}
                        </span>
                      </div>

                      {/* Linha 2: preview + canal + contador de não lidas */}
                      <div className="mt-0.5 flex items-center gap-2">
                        <p className="min-w-0 flex-1 truncate text-[13px] text-muted-foreground">
                          {conv.last_message_preview || 'Nova conversa iniciada'}
                        </p>
                        <div className="flex shrink-0 items-center gap-1.5">
                          {conv.unread_count > 0 && (
                            <span className="flex h-4 min-w-[18px] items-center justify-center rounded-full bg-danger px-1.5 text-[10px] font-bold text-primary-foreground">
                              {conv.unread_count}
                            </span>
                          )}
                        </div>
                      </div>

                      {/* S6 — agente responsável com mini-avatar */}
                      {conv.agent_name && (
                        <div className="mt-1 flex items-center gap-1.5">
                          <Avatar className="h-4 w-4 shrink-0 border border-border">
                            <AvatarFallback className="bg-primary/10 text-primary text-[8px] font-bold">
                              {conv.agent_name.substring(0, 2).toUpperCase()}
                            </AvatarFallback>
                          </Avatar>
                          <span className="min-w-0 truncate text-[10px] font-medium text-muted-foreground">
                            {conv.agent_name}
                          </span>
                        </div>
                      )}

                      {/* Linha 3 (condicional): badges de atendimento + indicadores de SLA/tempo */}
                      {(isHumanAttendance(conv.status) ||
                        conv.sla_health_status !== 'none' ||
                        conv.has_active_timer) && (
                        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                          {/* Badge HUMANO cobre os 3 estados humanos (§6.1/§12.3) */}
                          {isHumanAttendance(conv.status) && (
                            <Badge
                              className={`h-5 px-1.5 text-[9px] uppercase tracking-wide font-bold bg-danger text-primary-foreground border-0 ${
                                conv.status === 'HUMAN_REQUESTED' ? 'animate-pulse' : ''
                              }`}
                            >
                              Humano
                            </Badge>
                          )}

                          {/* Badge SLA vencido / em risco (§12.3) */}
                          {(() => {
                            const kind = slaBadgeKind(conv.sla_health_status);
                            if (kind === 'breached') {
                              return (
                                <Badge className="h-5 px-1.5 text-[9px] uppercase tracking-wide font-bold border-0 bg-danger text-primary-foreground">
                                  SLA vencido
                                </Badge>
                              );
                            }
                            if (kind === 'critical' || kind === 'at_risk') {
                              return (
                                <Badge
                                  className={`h-5 px-1.5 text-[9px] uppercase tracking-wide font-bold border ${
                                    kind === 'critical'
                                      ? 'bg-danger/10 text-danger border-danger/20'
                                      : 'bg-warning/10 text-warning border-warning/20'
                                  }`}
                                >
                                  SLA risco
                                </Badge>
                              );
                            }
                            return null;
                          })()}

                          {conv.sla_health_status !== 'none' && (
                            <SlaIndicator
                              sla={{ health_status: conv.sla_health_status, level: conv.sla_level }}
                              variant="compact"
                              className="shrink-0"
                            />
                          )}

                          {conv.has_active_timer && (
                            <span
                              className="flex items-center text-[10px] text-muted-foreground"
                              title="Encerramento automático agendado"
                            >
                              <Timer className="h-3 w-3" />
                            </span>
                          )}

                          {(() => {
                            // Seleção 1ª resposta vs resolução + supressão de none/paused:
                            // lógica PURA em pickListDeadline (testada no runner node).
                            const pick = pickListDeadline({
                              health: conv.sla_health_status,
                              firstResponseDeadline: conv.sla_first_response_deadline,
                              resolutionDeadline: conv.sla_resolution_deadline,
                              hasHumanReply: !!conv.last_human_message_at,
                            });
                            if (!pick) return null;
                            return (
                              <span
                                className={`flex items-center gap-0.5 text-[10px] ${
                                  pick.info.overdue ? 'text-danger' : 'text-muted-foreground'
                                }`}
                                title={`${pick.kind}: ${pick.info.overdue ? 'vencida há ' : 'faltam '}${pick.info.text}`}
                              >
                                <Clock className="h-3 w-3" />
                                {pick.info.text}
                              </span>
                            );
                          })()}
                        </div>
                      )}

                      {/* S6 — barra de progresso de SLA no rodapé */}
                      {(() => {
                        const progress = computeSlaProgress({
                          health: conv.sla_health_status,
                          startedAt: conv.sla_started_at,
                          firstResponseDeadline: conv.sla_first_response_deadline,
                          firstResponseAt: conv.sla_first_response_at,
                          resolutionDeadline: conv.sla_resolution_deadline,
                        });
                        if (!progress) return null;
                        return (
                          <SlaIndicator
                            variant="bar"
                            sla={null}
                            progress={progress}
                            className="mt-2"
                          />
                        );
                      })()}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </>
        )}
      </ConversationRail>

      {/* ================= PAINEL DIREITO (CHAT) ================= */}
      <ChatMain className="bg-background/50">
        {!selectedId ? (
          // --- EMPTY STATE (nenhuma conversa selecionada) ---
          <div className="flex flex-1 flex-col items-center justify-center p-12 text-center">
            <div className="mb-5 flex h-16 w-16 items-center justify-center rounded-2xl bg-primary/10">
              <MessageSquare className="h-7 w-7 text-primary" />
            </div>
            <h2 className="text-xl font-bold text-foreground">Nenhuma conversa selecionada</h2>
            <p className="mt-1 max-w-sm text-sm text-muted-foreground">
              Escolha uma fila no painel e selecione uma conversa para começar o atendimento.
            </p>
          </div>
        ) : (
          // --- MODO CHAT ---
          <>
            {/* Header Fixo */}
            <ChatTopbar>
              <div className="flex items-center gap-3">
                <Avatar className="h-9 w-9 border border-border">
                  <AvatarImage src={activeConversation?.user_avatar || undefined} />
                  <AvatarFallback className="bg-primary text-primary-foreground font-bold text-xs">
                    {(activeConversation?.user_name || 'U').substring(0, 2).toUpperCase()}
                  </AvatarFallback>
                </Avatar>
                <div>
                  <h3 className="text-sm font-bold text-foreground">
                    {activeConversation?.user_name}
                  </h3>
                  <div className="flex items-center gap-2">
                    {activeConversation && getChannelBadge(activeConversation.channel)}
                    {activeConversation?.user_phone && (
                      <>
                        <span className="w-1 h-1 rounded-full bg-muted-foreground" />
                        <span className="text-xs text-muted-foreground">
                          {activeConversation.user_phone}
                        </span>
                      </>
                    )}
                    {activeConversation?.agent_name && (
                      <>
                        <span className="w-1 h-1 rounded-full bg-muted-foreground" />
                        <Badge className="h-4 px-1.5 text-[9px] border-primary bg-primary text-primary-foreground">
                          {activeConversation.agent_name}
                        </Badge>
                      </>
                    )}
                  </div>
                </div>
              </div>

              <div className="flex items-center gap-2">
                {activeConversation?.status && (
                  <Badge
                    className={`text-xs px-2 py-1 ${
                      isHumanAttendance(activeConversation.status)
                        ? 'bg-danger/10 text-danger border-danger/20'
                        : activeConversation.status === 'RESOLVED'
                          ? 'bg-success/10 text-success border-success/20'
                          : activeConversation.status === 'CLOSED'
                            ? 'bg-muted text-muted-foreground border-border'
                            : 'bg-primary/10 text-primary border-primary/20'
                    }`}
                  >
                    {statusLabel(activeConversation.status)}
                  </Badge>
                )}
                {/* S9: botão de detalhes — abre o drawer do card em < 1280px. */}
                {/* No desktop (>= xl) o card já é coluna fixa, então o botão some. */}
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 text-muted-foreground hover:text-foreground xl:hidden"
                  onClick={() => setDetailsDrawerOpen(true)}
                  disabled={!selectedId}
                  title="Detalhes do atendimento"
                  aria-label="Detalhes do atendimento"
                >
                  <PanelRightOpen className="h-4 w-4" />
                </Button>
              </div>
            </ChatTopbar>

            {/* Área de Mensagens */}
            <ChatViewport className="space-y-6 bg-background/50" ref={scrollRef}>
              {isLoadingChat ? (
                <LoadingState label="Carregando mensagens..." className="h-full min-h-0" />
              ) : messages.length === 0 ? (
                <div className="flex flex-col items-center justify-center h-full text-muted-foreground space-y-2">
                  <MessageSquare className="w-8 h-8 opacity-20" />
                  <p className="text-sm">Nenhuma mensagem registrada.</p>
                </div>
              ) : (
                messages.map((msg, i) => {
                  const isUser = msg.role === 'user';

                  // FONTE ÚNICA da autoria humana (§22 item 3): consome o campo
                  // derivado `is_human` projetado por GET /api/messages
                  // (messageIsHuman). NÃO reimplementar a regra aqui — assim a
                  // mensagem humana legada (role='assistant'+sender_user_id, sem
                  // JOIN/prefixo) deixa de ser renderizada como IA.
                  const isHumanMessage = !!msg.is_human;

                  // Nome/avatar: usa sender do JOIN (FK users_v2) quando houver;
                  // fallback ao prefixo legado [👤 Nome] só para exibição.
                  const hasSenderFromDb = !!msg.sender_user_id && !!msg.sender;
                  const humanMatch =
                    !hasSenderFromDb && isHumanMessage
                      ? msg.content?.match(/^\[👤\s+(.+?)\]\n/)
                      : null;

                  const senderName = hasSenderFromDb
                    ? `${msg.sender?.first_name || ''} ${msg.sender?.last_name || ''}`.trim()
                    : humanMatch
                      ? humanMatch[1]
                      : null;
                  const senderAvatar = hasSenderFromDb ? msg.sender?.avatar_url : null;

                  // Agrupamento estilo WhatsApp: nome + foto 1x por grupo consecutivo do mesmo remetente
                  const prevMsg = i > 0 ? messages[i - 1] : null;
                  const isFirstOfGroup =
                    !prevMsg ||
                    prevMsg.role !== msg.role ||
                    prevMsg.sender_user_id !== msg.sender_user_id;

                  // Nome/foto do remetente (cabeçalho do grupo, estilo WhatsApp)
                  const groupName = isUser
                    ? activeConversation?.user_name || activeConversation?.user_phone || 'Usuário'
                    : isHumanMessage
                      ? senderName || adminName || 'Admin'
                      : activeConversation?.agent_name || 'Agente';
                  const groupAvatar = isUser
                    ? activeConversation?.user_avatar || undefined
                    : isHumanMessage
                      ? senderAvatar || adminAvatar || undefined
                      : undefined;

                  // Remove o prefixo do conteúdo para exibição (retrocompatibilidade)
                  let displayContent = humanMatch
                    ? msg.content.replace(/^\[👤\s+.+?\]\n/, '')
                    : msg.content;

                  // Detectar e extrair conteúdo de comércio (TODOS os blocos UCP)
                  let ucpDataList: UCPData[] = [];
                  if (!isUser && displayContent) {
                    const extracted = extractAllUCPData(displayContent);
                    displayContent = extracted.text;
                    ucpDataList = extracted.dataList;

                    // Se nenhum bloco completo foi parseado mas há JSON UCP parcial
                    // (streaming em andamento), esconde todo o JSON parcial do display.
                    if (displayContent) {
                      const partialUcpMatch = displayContent.match(/\{\s*("|')type\1\s*:\s*\1ucp_/);
                      if (partialUcpMatch && partialUcpMatch.index !== undefined) {
                        displayContent = displayContent.substring(0, partialUcpMatch.index).trim();
                      }
                    }
                  }

                  // Remove textos desnecessários de mídia
                  const isMediaOnly =
                    displayContent === '📷 Imagem enviada' ||
                    displayContent === '🎤 Áudio enviado' ||
                    displayContent === '[Mensagem de voz]';
                  const hasAudio = !!msg.audio_url;
                  const hasImage = !!msg.image_url;

                  return (
                    <div
                      key={msg.id}
                      className={`flex w-full items-start gap-2 ${isUser ? 'flex-row' : 'flex-row-reverse'}`}
                    >
                      {/* 📷 Foto do remetente (estilo WhatsApp): só na 1ª msg do grupo, no TOPO ao lado do balão; spacer alinha as demais */}
                      <div className="w-8 shrink-0">
                        {isFirstOfGroup && (
                          <Avatar className="h-8 w-8 border border-border">
                            <AvatarImage src={groupAvatar} />
                            <AvatarFallback
                              className={`text-[10px] font-bold ${
                                isUser
                                  ? 'bg-secondary text-foreground'
                                  : 'bg-primary text-primary-foreground'
                              }`}
                            >
                              {groupName.substring(0, 2).toUpperCase()}
                            </AvatarFallback>
                          </Avatar>
                        )}
                      </div>

                      <div
                        className={`flex min-w-0 flex-col ${ucpDataList.length > 0 ? 'max-w-[95%]' : 'max-w-[80%]'} ${
                          isUser ? 'items-start' : 'items-end'
                        }`}
                      >
                        <div
                          className={`min-w-0 rounded-2xl px-4 py-3 text-sm leading-relaxed ${
                            isUser
                              ? 'bg-secondary text-foreground border border-border rounded-tl-sm'
                              : 'bg-primary chat-bubble-sent text-white rounded-tr-sm'
                          }`}
                        >
                          {/* 🔔 Nome do remetente DENTRO do balão (1x por grupo, estilo WhatsApp) */}
                          {isFirstOfGroup && (
                            <span
                              className={`mb-1 block text-xs font-bold ${
                                isUser ? 'text-primary' : 'text-white'
                              }`}
                            >
                              {groupName}
                            </span>
                          )}
                          {/* 🖼️ Imagem */}
                          {hasImage && (
                            <div className="mb-2 rounded-lg overflow-hidden border border-border">
                              <img
                                src={msg.image_url}
                                alt="Anexo"
                                className="max-w-full h-auto max-h-[300px] object-cover cursor-pointer hover:opacity-90 transition-opacity"
                                onClick={() => window.open(msg.image_url, '_blank')}
                              />
                            </div>
                          )}

                          {/* 🎤 Áudio com player */}
                          {hasAudio && (
                            <div className="mb-2">
                              <VoiceMessage
                                audioUrl={msg.audio_url!}
                                transcription={!isMediaOnly ? displayContent : undefined}
                              />
                            </div>
                          )}

                          {/* 🛒 UCP Content - um carrossel/card/checkout por bloco (2+ buscas no mesmo turno) */}
                          {ucpDataList.map((ucp, ucpIdx) => (
                            <div key={ucpIdx} className="w-full mt-2">
                              {ucp.type === 'ucp_product_list' && (
                                <ProductCarousel
                                  products={ucp.products}
                                  shopDomain={ucp.shop_domain}
                                  query={ucp.query}
                                />
                              )}
                              {ucp.type === 'ucp_product_detail' && (
                                <ProductCard product={ucp.product} size="large" />
                              )}
                              {ucp.type === 'ucp_checkout' && <CheckoutButton data={ucp} />}
                            </div>
                          ))}

                          {/* 📝 Texto + horário (estilo WhatsApp: hora na MESMA linha da última frase, colada no canto inferior-direito) */}
                          <div className="flex flex-wrap items-end gap-x-2">
                            {!isMediaOnly && !hasAudio && displayContent && (
                              <div
                                className={`prose prose-sm max-w-none min-w-0 [&>*:last-child]:!mb-0 [&_ul]:list-disc [&_ol]:list-decimal [&_li]:ml-4 ${
                                  isUser
                                    ? 'text-foreground dark:prose-invert [&_a]:text-primary [&_a]:underline'
                                    : 'chat-admin-bubble-text [&_a]:underline'
                                }`}
                              >
                                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                                  {displayContent}
                                </ReactMarkdown>
                              </div>
                            )}
                            <span
                              className={`ml-auto flex shrink-0 translate-y-[3px] items-center gap-1 text-[10px] leading-none ${isUser ? 'text-muted-foreground' : 'text-white/70'}`}
                            >
                              {new Date(msg.created_at).toLocaleTimeString([], {
                                hour: '2-digit',
                                minute: '2-digit',
                              })}
                              {!isUser && <CheckCheck className="w-3 h-3 opacity-70" />}
                            </span>
                          </div>
                        </div>
                      </div>
                    </div>
                  );
                })
              )}
            </ChatViewport>

            {/* Área do Input */}
            <ChatComposerDock className="bg-card">
              {isHumanAttendance(activeConversation?.status) ? (
                /* 🔔 INPUT HABILITADO - Atendimento Humano */
                <>
                  {/* Hidden file input */}
                  <input
                    type="file"
                    accept="image/*"
                    ref={fileInputRef}
                    onChange={handleImageSelect}
                    className="hidden"
                  />

                  <div className="flex items-center gap-2 max-w-4xl mx-auto">
                    {/* Botão de Imagem */}
                    <Button
                      size="icon"
                      variant="ghost"
                      onClick={() => fileInputRef.current?.click()}
                      disabled={isUploadingMedia || isRecording}
                      className="h-10 w-10 text-muted-foreground hover:text-foreground hover:bg-muted"
                      title="Enviar imagem"
                    >
                      {isUploadingMedia ? (
                        <Loader2 className="w-5 h-5 animate-spin" />
                      ) : (
                        <ImageIcon className="w-5 h-5" />
                      )}
                    </Button>

                    {/* Botão de Áudio */}
                    <Button
                      size="icon"
                      variant="ghost"
                      onClick={isRecording ? stopRecording : startRecording}
                      disabled={isUploadingMedia}
                      className={`h-10 w-10 ${
                        isRecording
                          ? 'text-danger bg-danger/10 hover:bg-danger/30 animate-pulse'
                          : 'text-muted-foreground hover:text-foreground hover:bg-muted'
                      }`}
                      title={isRecording ? 'Parar gravação' : 'Gravar áudio'}
                    >
                      {isRecording ? <Square className="w-5 h-5" /> : <Mic className="w-5 h-5" />}
                    </Button>

                    {/* Input de Texto */}
                    <div className="relative flex-1">
                      <Input
                        ref={humanReplyInputRef}
                        placeholder={isRecording ? 'Gravando...' : 'Digite sua resposta...'}
                        value={humanReplyText}
                        onChange={(e) => setHumanReplyText(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' && !e.shiftKey && humanReplyText.trim()) {
                            handleSendHumanReply();
                          }
                        }}
                        disabled={isRecording}
                        className="bg-muted/50 border-border pr-12 text-foreground"
                      />
                      <Button
                        size="icon"
                        disabled={isSendingReply || !humanReplyText.trim() || isRecording}
                        onClick={handleSendHumanReply}
                        className="absolute right-1 top-1 h-8 w-8 bg-primary hover:bg-primary/90 text-primary-foreground"
                      >
                        <Send className="w-4 h-4" />
                      </Button>
                    </div>
                  </div>
                  <div className="flex items-center justify-between max-w-4xl mx-auto mt-2">
                    <p className="text-[10px] text-danger font-medium">
                      Atendimento por <span className="font-bold">{adminName}</span> - Mensagens
                      diretas ao usuário
                    </p>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={handleCloseHumanHandoff}
                      className="h-8 text-xs px-3 bg-primary text-primary-foreground hover:bg-primary/90 border-0"
                    >
                      Devolver para IA
                    </Button>
                  </div>
                </>
              ) : (
                /* INPUT DESABILITADO - Modo Visualização com botão ASSUMIR */
                <>
                  <div className="relative max-w-4xl mx-auto">
                    <Input
                      placeholder="Intervenção Humana (apenas para conversas solicitadas)"
                      disabled
                      className="bg-muted/50 border-border pr-12 text-muted-foreground"
                    />
                    <Button
                      size="icon"
                      disabled
                      className="absolute right-1 top-1 h-8 w-8 bg-primary opacity-50 text-primary-foreground"
                    >
                      <Send className="w-4 h-4" />
                    </Button>
                  </div>
                  <div className="flex items-center justify-between max-w-4xl mx-auto mt-2">
                    <p className="text-[10px] text-muted-foreground font-medium">
                      O agente está respondendo automaticamente.
                    </p>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={handleTakeOver}
                      className="h-8 text-xs px-3 bg-primary text-primary-foreground hover:bg-primary/90 border-0"
                    >
                      <Hand className="w-4 h-4 mr-1.5" />
                      Assumir
                    </Button>
                  </div>
                </>
              )}
            </ChatComposerDock>
          </>
        )}
      </ChatMain>

      {/* ================= TERCEIRA COLUNA: CARD LATERAL (S9, §12.1) ================= */}
      {/* Desktop >= 1280px: coluna fixa irmã de ChatMain. Sem conversa selecionada */}
      {/* NÃO renderiza no desktop (botão de detalhes fica desabilitado no topbar). */}
      {selectedId && (
        <ChatDetailsAside>
          <ConversationDetailsPanel
            conversationId={selectedId}
            companyId={effectiveCompanyId}
            details={details}
            isLoading={isLoadingDetails}
            error={detailsError}
            onRefresh={refetchDetails}
            onListRefresh={refetchList}
          />
        </ChatDetailsAside>
      )}

      {/* < 1280px: o mesmo card vira drawer/overlay acionado pelo botão do topbar. */}
      <Sheet open={detailsDrawerOpen} onOpenChange={setDetailsDrawerOpen}>
        <SheetContent side="right" className="w-[360px] max-w-[90vw] p-0 xl:hidden">
          <SheetHeader className="border-b border-border px-4 py-3">
            <SheetTitle>Detalhes do atendimento</SheetTitle>
          </SheetHeader>
          <div className="h-[calc(100%-3.25rem)]">
            <ConversationDetailsPanel
              conversationId={selectedId}
              companyId={effectiveCompanyId}
              details={details}
              isLoading={isLoadingDetails}
              error={detailsError}
              onRefresh={refetchDetails}
              onListRefresh={refetchList}
            />
          </div>
        </SheetContent>
      </Sheet>
    </ChatFrame>
  );
}

export default function AdminConversationsPage() {
  return (
    <Suspense fallback={<LoadingState />}>
      <AdminConversationsPageInner />
    </Suspense>
  );
}
