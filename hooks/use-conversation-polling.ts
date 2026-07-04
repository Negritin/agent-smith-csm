'use client';

/**
 * S9 — Polling AUTENTICADO da inbox de atendimento (SPEC §19 Fase 5 item 2, D2,
 * §17 item 7).
 *
 * PRÉ-REQUISITO do S11 (fechamento do `anon`). Substitui, como FONTE DE
 * ATUALIZAÇÃO da UI, a subscription Supabase Realtime ANÔNIMA que hoje vive em
 * `app/admin/conversations/page.tsx`
 * (`supabase.channel('admin-inbox').on('postgres_changes', …)` sobre
 * `conversations`/`messages`). Aqui consumimos as rotas AUTENTICADAS por
 * iron-session:
 *   - `GET /api/admin/conversations`            (lista enriquecida — S7)
 *   - `GET /api/admin/conversations/[id]/details` (card lateral — S7)
 *
 * Características:
 *   - intervalo curto configurável (default 4s) com cleanup;
 *   - backoff exponencial em erro (até `maxIntervalMs`);
 *   - PAUSA quando a aba está inativa (`visibilitychange`) e refetch imediato ao
 *     voltar; (poupa rede/CPU multi-tenant);
 *   - cancelamento via `AbortController` ao trocar de conversa / desmontar (sem
 *     setState após unmount; sem corrida entre conversas).
 *
 * META S9 (§17 item 7): após este hook, NENHUM caminho do admin depende da
 * subscription `anon` para atualizar lista/card. A subscription só é REMOVIDA do
 * banco no S11; aqui o front apenas para de DEPENDER dela.
 *
 * Helpers de cálculo de delay são exportados PUROS (`nextPollDelay`) para teste
 * no runner `node` do vitest, sem timers reais.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type {
  ConversationDetails,
  ConversationListItem,
  ConversationListResponse,
} from '@/types/conversation-details';

// =========================================================================== //
// Lógica pura (testável sem DOM/timers)
// =========================================================================== //

export type PollBackoffConfig = {
  /** Intervalo base quando tudo está OK (ms). */
  baseIntervalMs: number;
  /** Teto do backoff em erro (ms). */
  maxIntervalMs: number;
  /** Fator multiplicativo do backoff. */
  factor: number;
};

export const DEFAULT_BACKOFF: PollBackoffConfig = {
  baseIntervalMs: 4000,
  maxIntervalMs: 30000,
  factor: 2,
};

/**
 * Calcula o próximo delay de polling. Sucesso ⇒ reseta para `baseIntervalMs`.
 * Erro ⇒ multiplica o atual por `factor`, limitado a `maxIntervalMs`.
 */
export function nextPollDelay(
  currentMs: number,
  ok: boolean,
  cfg: PollBackoffConfig = DEFAULT_BACKOFF,
): number {
  if (ok) return cfg.baseIntervalMs;
  const grown = Math.max(cfg.baseIntervalMs, currentMs) * cfg.factor;
  return Math.min(cfg.maxIntervalMs, grown);
}

/** Query string canônica da lista a partir de filtros (§12.3). */
export type ConversationListFilters = {
  channel?: string;
  status?: string;
  sla_status?: string;
  agent_id?: string;
  assigned_user_id?: string;
  /** Deep-link F1.5: id do contato (= conversations.user_id). Campo SEPARADO de assigned_user_id. */
  contact_user_id?: string;
  search?: string;
};

export function buildListQuery(filters: ConversationListFilters | undefined): string {
  if (!filters) return '';
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v && v !== 'all') sp.set(k, v);
  }
  const q = sp.toString();
  return q ? `?${q}` : '';
}

// =========================================================================== //
// Hook: polling da LISTA
// =========================================================================== //

type UseConversationListPollingOptions = {
  enabled?: boolean;
  filters?: ConversationListFilters;
  backoff?: PollBackoffConfig;
};

type ConversationListPollingResult = {
  conversations: ConversationListItem[];
  isLoading: boolean;
  error: string | null;
  /** Força um refetch imediato (ex.: após uma ação ou clique em "atualizar"). */
  refetch: () => void;
};

export function useConversationListPolling(
  options: UseConversationListPollingOptions = {},
): ConversationListPollingResult {
  const { enabled = true, filters, backoff = DEFAULT_BACKOFF } = options;

  const [conversations, setConversations] = useState<ConversationListItem[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Refs estáveis p/ o loop não recriar a cada render.
  const delayRef = useRef(backoff.baseIntervalMs);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);
  const filtersRef = useRef(filters);
  filtersRef.current = filters;

  const clearTimer = () => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  };

  const tick = useCallback(async () => {
    // Cancela qualquer requisição da lista ainda em voo.
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    let ok = false;
    try {
      const res = await fetch(`/api/admin/conversations${buildListQuery(filtersRef.current)}`, {
        credentials: 'include',
        signal: controller.signal,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as ConversationListResponse;
      if (!mountedRef.current) return;
      setConversations(Array.isArray(data.conversations) ? data.conversations : []);
      setError(null);
      ok = true;
    } catch (err) {
      if ((err as { name?: string })?.name === 'AbortError') return;
      if (!mountedRef.current) return;
      setError('Falha ao atualizar conversas');
    } finally {
      if (mountedRef.current) setIsLoading(false);
    }

    if (!mountedRef.current) return;
    delayRef.current = nextPollDelay(delayRef.current, ok, backoff);
    schedule();
  }, [backoff]);

  const schedule = useCallback(() => {
    clearTimer();
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') {
      // Aba inativa: NÃO agenda (visibilitychange retoma). §S9 backoff/pausa.
      return;
    }
    timerRef.current = setTimeout(() => {
      void tick();
    }, delayRef.current);
  }, [tick]);

  const refetch = useCallback(() => {
    delayRef.current = backoff.baseIntervalMs;
    void tick();
  }, [tick, backoff.baseIntervalMs]);

  useEffect(() => {
    mountedRef.current = true;
    if (!enabled) {
      setIsLoading(false);
      return () => {
        mountedRef.current = false;
      };
    }

    delayRef.current = backoff.baseIntervalMs;
    void tick(); // fetch imediato

    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        delayRef.current = backoff.baseIntervalMs;
        void tick(); // refetch imediato ao voltar
      } else {
        clearTimer(); // pausa enquanto oculta
      }
    };
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      mountedRef.current = false;
      clearTimer();
      abortRef.current?.abort();
      document.removeEventListener('visibilitychange', onVisibility);
    };
    // Reinicia o loop quando habilita/desabilita ou os filtros mudam.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, JSON.stringify(filters)]);

  return { conversations, isLoading, error, refetch };
}

// =========================================================================== //
// Hook: polling do CARD (/details) da conversa aberta
// =========================================================================== //

type UseConversationDetailsPollingOptions = {
  conversationId: string | null;
  enabled?: boolean;
  backoff?: PollBackoffConfig;
};

type ConversationDetailsPollingResult = {
  details: ConversationDetails | null;
  isLoading: boolean;
  error: string | null;
  refetch: () => void;
};

export function useConversationDetailsPolling(
  options: UseConversationDetailsPollingOptions,
): ConversationDetailsPollingResult {
  const { conversationId, enabled = true, backoff = DEFAULT_BACKOFF } = options;

  const [details, setDetails] = useState<ConversationDetails | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const delayRef = useRef(backoff.baseIntervalMs);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);
  const idRef = useRef(conversationId);
  idRef.current = conversationId;

  const clearTimer = () => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  };

  const tick = useCallback(async () => {
    const id = idRef.current;
    if (!id) return;

    // Cancela a requisição anterior (troca de conversa => não aplica resposta velha).
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    let ok = false;
    try {
      const res = await fetch(`/api/admin/conversations/${id}/details`, {
        credentials: 'include',
        signal: controller.signal,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as ConversationDetails;
      // Guarda contra resposta de uma conversa que já não está selecionada.
      if (!mountedRef.current || idRef.current !== id) return;
      setDetails(data);
      setError(null);
      ok = true;
    } catch (err) {
      if ((err as { name?: string })?.name === 'AbortError') return;
      if (!mountedRef.current || idRef.current !== id) return;
      setError('Falha ao carregar detalhes do atendimento');
    } finally {
      if (mountedRef.current && idRef.current === id) setIsLoading(false);
    }

    if (!mountedRef.current || idRef.current !== id) return;
    delayRef.current = nextPollDelay(delayRef.current, ok, backoff);
    schedule();
  }, [backoff]);

  const schedule = useCallback(() => {
    clearTimer();
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
    timerRef.current = setTimeout(() => {
      void tick();
    }, delayRef.current);
  }, [tick]);

  const refetch = useCallback(() => {
    delayRef.current = backoff.baseIntervalMs;
    void tick();
  }, [tick, backoff.baseIntervalMs]);

  useEffect(() => {
    mountedRef.current = true;

    if (!enabled || !conversationId) {
      // Trocou para "sem conversa": limpa o card e o loop.
      clearTimer();
      abortRef.current?.abort();
      setDetails(null);
      setError(null);
      setIsLoading(false);
      return () => {
        mountedRef.current = false;
      };
    }

    // Nova conversa selecionada: estado limpo + fetch imediato.
    setDetails(null);
    setError(null);
    setIsLoading(true);
    delayRef.current = backoff.baseIntervalMs;
    void tick();

    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        delayRef.current = backoff.baseIntervalMs;
        void tick();
      } else {
        clearTimer();
      }
    };
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      mountedRef.current = false;
      clearTimer();
      abortRef.current?.abort();
      document.removeEventListener('visibilitychange', onVisibility);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId, enabled]);

  return { details, isLoading, error, refetch };
}
