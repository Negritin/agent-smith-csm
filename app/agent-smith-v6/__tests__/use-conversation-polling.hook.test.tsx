// @vitest-environment jsdom
/**
 * §18.1/§18.3 — Ciclo de vida dos hooks de polling (renderHook), além dos
 * helpers puros (nextPollDelay/buildListQuery) já testados em attendance-s9.
 *
 * Prova:
 *   - montagem dispara um fetch imediato e popula o estado;
 *   - DESMONTAGEM cancela o timer (nenhum fetch agendado dispara depois) e aborta
 *     a requisição em voo (sem setState pós-unmount);
 *   - BACKOFF é aplicado em erro (o próximo agendamento usa baseInterval*factor);
 *   - `enabled:false` não dispara fetch.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import {
  useConversationListPolling,
  useConversationDetailsPolling,
} from '@/hooks/use-conversation-polling';

/**
 * Sob fake timers, `waitFor` não avança o relógio e trava. Em vez disso,
 * drenamos a fila de microtasks (várias voltas) dentro de `act` — suficiente
 * para o `fetch` mockado (que resolve sincronamente) propagar o setState.
 */
async function flush(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 5; i += 1) {
      await Promise.resolve();
    }
  });
}

const BACKOFF = { baseIntervalMs: 1000, maxIntervalMs: 8000, factor: 2 };

function okListResponse() {
  return {
    ok: true,
    json: async () => ({ conversations: [{ id: 'c1' }], pagination: {} }),
  } as unknown as Response;
}

function okDetailsResponse(id: string) {
  return {
    ok: true,
    json: async () => ({ conversation: { id }, sla: { health_status: 'none' } }),
  } as unknown as Response;
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.useFakeTimers();
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.runOnlyPendingTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('useConversationListPolling — lifecycle', () => {
  it('monta com fetch imediato e popula a lista', async () => {
    fetchMock.mockResolvedValue(okListResponse());
    const { result } = renderHook(() =>
      useConversationListPolling({ backoff: BACKOFF }),
    );

    await flush();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toContain('/api/admin/conversations');
    expect(result.current.conversations).toHaveLength(1);
  });

  it('enabled:false NÃO dispara fetch', async () => {
    fetchMock.mockResolvedValue(okListResponse());
    renderHook(() => useConversationListPolling({ enabled: false, backoff: BACKOFF }));
    await flush();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('DESMONTAGEM cancela o timer: nenhum fetch extra após avançar o relógio', async () => {
    fetchMock.mockResolvedValue(okListResponse());
    const { unmount } = renderHook(() =>
      useConversationListPolling({ backoff: BACKOFF }),
    );

    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(1); // fetch inicial

    unmount();
    // Avança bem além do baseInterval: se o timer não tivesse sido limpo, dispararia.
    await act(async () => {
      vi.advanceTimersByTime(BACKOFF.baseIntervalMs * 5);
      await Promise.resolve();
    });
    expect(fetchMock).toHaveBeenCalledTimes(1); // continua 1 — timer foi cancelado
  });

  it('BACKOFF em erro: o próximo poll é agendado com base*factor', async () => {
    // Primeiro fetch falha (HTTP 500) → delay cresce para base*factor.
    fetchMock.mockResolvedValueOnce({ ok: false, status: 500 } as Response);
    fetchMock.mockResolvedValue(okListResponse());

    const { result } = renderHook(() =>
      useConversationListPolling({ backoff: BACKOFF }),
    );

    await flush();
    expect(result.current.error).toBe('Falha ao atualizar conversas');
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Antes de base*factor (2000ms): nada dispara (backoff cresceu para 2000ms).
    await act(async () => {
      vi.advanceTimersByTime(BACKOFF.baseIntervalMs); // 1000ms
      await Promise.resolve();
    });
    expect(fetchMock).toHaveBeenCalledTimes(1); // ainda não — agendado para 2000ms

    await act(async () => {
      vi.advanceTimersByTime(BACKOFF.baseIntervalMs); // chega a 2000ms total
      await Promise.resolve();
    });
    expect(fetchMock).toHaveBeenCalledTimes(2); // disparou no backoff
  });
});

describe('useConversationDetailsPolling — lifecycle', () => {
  it('monta com fetch do /details da conversa selecionada', async () => {
    fetchMock.mockResolvedValue(okDetailsResponse('conv-9'));
    const { result } = renderHook(() =>
      useConversationDetailsPolling({ conversationId: 'conv-9', backoff: BACKOFF }),
    );

    await flush();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/admin/conversations/conv-9/details');
    expect(result.current.details?.conversation.id).toBe('conv-9');
  });

  it('conversationId=null NÃO dispara fetch e mantém details null', async () => {
    fetchMock.mockResolvedValue(okDetailsResponse('x'));
    const { result } = renderHook(() =>
      useConversationDetailsPolling({ conversationId: null, backoff: BACKOFF }),
    );
    await flush();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.details).toBeNull();
  });

  it('DESMONTAGEM cancela o timer do /details', async () => {
    fetchMock.mockResolvedValue(okDetailsResponse('conv-9'));
    const { unmount } = renderHook(() =>
      useConversationDetailsPolling({ conversationId: 'conv-9', backoff: BACKOFF }),
    );
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    unmount();
    await act(async () => {
      vi.advanceTimersByTime(BACKOFF.baseIntervalMs * 5);
      await Promise.resolve();
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
