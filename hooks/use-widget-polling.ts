'use client';

/**
 * S9 [validador — WIDGET] — Polling do chat PÚBLICO (widget) via RPC escopada
 * (SPEC §17 item 7, §18.2; SPRINTS S9 entregável 2).
 *
 * PRÉ-REQUISITO do REVOKE de S11: o widget NÃO pode depender da subscription
 * Supabase Realtime ANÔNIMA em `messages`. Este hook atualiza o widget por
 * POLLING da rota pública `GET /api/widget/messages?session_id=…`, que no servidor
 * chama a RPC escopada `get_widget_messages_scoped` (concedida a `anon` por
 * `20260528_widget_messages_scoped_rpc.sql` e re-concedida pelo hotfix
 * `20260528_widget_hmac_private_secret_hotfix.sql`; PRESERVADA em S11 — só a
 * leitura ampla `anon` e a policy de realtime aberta são fechadas).
 *
 * FONTE ÚNICA (S9 entregável 2): `app/embed/[agentId]/EmbedChatClient.tsx`
 * CONSOME este hook (não usa `supabase.channel`/realtime `anon`), logo o widget
 * está livre do `anon`. Centralizar o loop AQUI garante que novos consumidores do
 * widget não reintroduzam a dependência de realtime `anon`. Critério de aceite
 * empírico (§S9): o widget atualiza com a subscription `anon` inativa.
 *
 * `nextWidgetPollDelay` é exportado PURO para teste no runner `node` do vitest.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

export type WidgetPollMessage = {
  id: string;
  role: string;
  content: string | null;
  image_url?: string | null;
  created_at: string;
  sender?: {
    first_name?: string | null;
    last_name?: string | null;
    avatar_url?: string | null;
  } | null;
};

export type WidgetPollResponse = {
  messages: WidgetPollMessage[];
  status: string;
  conversationId: string;
};

const BASE_INTERVAL_MS = 3000;
const MAX_INTERVAL_MS = 30000;

/**
 * Backoff do widget: novidade ⇒ reseta para 3s (resposta rápida do atendente);
 * sem novidade/erro ⇒ dobra até 30s. (Espelha o EmbedChatClient existente.)
 */
export function nextWidgetPollDelay(currentMs: number, hadNews: boolean): number {
  if (hadNews) return BASE_INTERVAL_MS;
  return Math.min(MAX_INTERVAL_MS, Math.max(BASE_INTERVAL_MS, currentMs) * 2);
}

type UseWidgetPollingOptions = {
  sessionId: string | null;
  /** Token HMAC do widget (enviado em `X-Widget-Token`). */
  widgetToken: string | null;
  /**
   * Provedor de token escopado. Chamado proativamente antes de cada poll quando
   * o token está ausente ou perto de expirar, e em resposta a 401. Deve retornar
   * o token novo (ou null se indisponível). Centraliza aqui o refresh que antes
   * vivia inline no EmbedChatClient.
   */
  refreshToken?: (sessionId: string) => Promise<string | null>;
  /**
   * Timestamp ISO de expiração do token atual. Quando faltar menos que
   * `refreshThresholdMs` para expirar, o token é renovado ANTES do poll
   * (espelha o refresh proativo do EmbedChatClient: `<60s` para expirar).
   */
  widgetTokenExpiresAt?: string | null;
  /** Liga/desliga o loop (ex.: só quando o chat está aberto e não enviando). */
  enabled?: boolean;
  /** Janela antes da expiração em que o token é renovado proativamente. */
  refreshThresholdMs?: number;
  /**
   * Recebe as mensagens do servidor a cada poll bem-sucedido, junto com um flag
   * `hadNews` (houve mensagem nova vs. o snapshot anterior do hook). O CONSUMIDOR
   * mescla no próprio estado, preservando bolhas otimistas/streaming. Mantém o
   * componente como dono do `messages` (necessário p/ optimistic + persistência
   * em localStorage), com o hook como fonte única do LOOP.
   */
  onMessages?: (messages: WidgetPollMessage[], meta: { hadNews: boolean }) => void;
};

type UseWidgetPollingResult = {
  status: string | null;
  conversationId: string | null;
  refetch: () => void;
};

export function useWidgetPolling(options: UseWidgetPollingOptions): UseWidgetPollingResult {
  const {
    sessionId,
    widgetToken,
    refreshToken,
    widgetTokenExpiresAt = null,
    enabled = true,
    refreshThresholdMs = 60 * 1000,
    onMessages,
  } = options;

  const [status, setStatus] = useState<string | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);

  const delayRef = useRef(BASE_INTERVAL_MS);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);
  // Espelha `enabled` a cada render. O reschedule de um poll EM VOO precisa
  // checar isto (não só `mountedRef`), pois `mountedRef.current` é resetado para
  // `true` no topo de cada re-run do efeito — inclusive no re-run com
  // `enabled=false`. Sem este guard, um poll cuja fetch estava pendente quando
  // `enabled` virou false ressuscita o loop ao resolver. `enabled` alterna a
  // cada envio (consumidor: `!!sessionId && isOpen && !sending`), então o cenário
  // é quente em produção.
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;
  const tokenRef = useRef(widgetToken);
  tokenRef.current = widgetToken;
  const expiresAtRef = useRef(widgetTokenExpiresAt);
  expiresAtRef.current = widgetTokenExpiresAt;
  const onMessagesRef = useRef(onMessages);
  onMessagesRef.current = onMessages;
  // Último snapshot servido — base para o flag `hadNews` (não substitui o estado
  // do consumidor, só detecta novidade para o backoff e para o callback).
  const lastSnapshotRef = useRef<WidgetPollMessage[]>([]);

  const clearTimer = () => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  };

  const poll = useCallback(async () => {
    if (!sessionId) return;

    let token = tokenRef.current;
    // Refresh proativo: token ausente OU perto de expirar.
    const expiresAt = expiresAtRef.current;
    const nearExpiry = !expiresAt || Date.parse(expiresAt) - Date.now() < refreshThresholdMs;
    if (refreshToken && (!token || nearExpiry)) {
      const refreshed = await refreshToken(sessionId);
      if (refreshed) token = refreshed;
    }
    if (!token) {
      // Sem token e sem como renovar: re-arma o loop para tentar de novo quando
      // o token chegar (evita travar o polling até enabled/sessionId mudar).
      if (!mountedRef.current || !enabledRef.current) return;
      delayRef.current = nextWidgetPollDelay(delayRef.current, false);
      clearTimer();
      timerRef.current = setTimeout(() => void poll(), delayRef.current);
      return;
    }

    let hadNews = false;
    try {
      const res = await fetch(`/api/widget/messages?session_id=${encodeURIComponent(sessionId)}`, {
        headers: { 'X-Widget-Token': token },
      });
      if (res.status === 401 && refreshToken) {
        await refreshToken(sessionId);
        // Não retorna sem re-armar (cai no agendamento abaixo).
      } else if (res.ok) {
        const data = (await res.json()) as WidgetPollResponse;
        if (!mountedRef.current) return;

        const incoming = Array.isArray(data.messages) ? data.messages : [];
        const prev = lastSnapshotRef.current;
        // Anti-flicker: novidade só quando há MAIS mensagens ou a última mudou.
        if (
          incoming.length > prev.length ||
          (incoming.length > 0 && incoming[incoming.length - 1]?.id !== prev[prev.length - 1]?.id)
        ) {
          hadNews = true;
          lastSnapshotRef.current = incoming;
        }
        onMessagesRef.current?.(incoming, { hadNews });
        setStatus(data.status ?? null);
        setConversationId(data.conversationId ?? null);
      }
    } catch {
      // best-effort; backoff em erro também
    }

    if (!mountedRef.current || !enabledRef.current) return;
    delayRef.current = nextWidgetPollDelay(delayRef.current, hadNews);
    clearTimer();
    timerRef.current = setTimeout(() => void poll(), delayRef.current);
  }, [sessionId, refreshToken, refreshThresholdMs]);

  const refetch = useCallback(() => {
    delayRef.current = BASE_INTERVAL_MS;
    clearTimer();
    void poll();
  }, [poll]);

  useEffect(() => {
    mountedRef.current = true;
    if (!enabled || !sessionId) {
      clearTimer();
      return () => {
        mountedRef.current = false;
        clearTimer();
      };
    }

    delayRef.current = BASE_INTERVAL_MS;
    lastSnapshotRef.current = [];
    void poll();

    // PAUSA quando a aba está oculta; retoma (poll imediato) ao voltar.
    const onVisibility = () => {
      if (typeof document === 'undefined') return;
      if (document.visibilityState === 'visible') {
        delayRef.current = BASE_INTERVAL_MS;
        clearTimer();
        void poll();
      } else {
        clearTimer();
      }
    };
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibility);
    }

    return () => {
      mountedRef.current = false;
      clearTimer();
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisibility);
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, enabled, poll]);

  return { status, conversationId, refetch };
}
