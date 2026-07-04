// @vitest-environment jsdom
/**
 * §18.2/§18.3 — Ciclo de vida do hook de polling do WIDGET (renderHook), além do
 * helper puro `nextWidgetPollDelay` já testado em attendance-s10.
 *
 * Prova:
 *   - montagem com token dispara o poll da rota pública e expõe status/conversa
 *     + entrega mensagens ao consumidor via onMessages (caminho que substitui o
 *       realtime anônimo após o REVOKE de S11);
 *   - DESMONTAGEM cancela o timer (sem poll extra ao avançar o relógio);
 *   - enabled:false / sessionId:null não dispara poll;
 *   - sem token e sem refreshToken, o poll não chama fetch.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { useWidgetPolling } from '@/hooks/use-widget-polling';

async function flush(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 6; i += 1) {
      await Promise.resolve();
    }
  });
}

function okWidgetResponse() {
  return {
    ok: true,
    status: 200,
    json: async () => ({
      messages: [{ id: 'm1', role: 'human', content: 'olá', created_at: '2026-01-01T00:00:00Z' }],
      status: 'HUMAN_ACTIVE',
      conversationId: 'conv-w',
    }),
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

describe('useWidgetPolling — lifecycle', () => {
  it('monta com token e entrega mensagens + status via poll', async () => {
    fetchMock.mockResolvedValue(okWidgetResponse());
    const onMessages = vi.fn();
    const { result } = renderHook(() =>
      useWidgetPolling({
        sessionId: 'sess-w',
        widgetToken: 'tok-1',
        widgetTokenExpiresAt: new Date(Date.now() + 10 * 60 * 1000).toISOString(),
        onMessages,
      }),
    );

    await flush();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toContain('/api/widget/messages?session_id=sess-w');
    expect(result.current.status).toBe('HUMAN_ACTIVE');
    expect(result.current.conversationId).toBe('conv-w');
    expect(onMessages).toHaveBeenCalledTimes(1);
    expect(onMessages.mock.calls[0][0]).toHaveLength(1);
    expect(onMessages.mock.calls[0][1]).toEqual({ hadNews: true });
  });

  it('enabled:false NÃO dispara poll', async () => {
    fetchMock.mockResolvedValue(okWidgetResponse());
    renderHook(() =>
      useWidgetPolling({ sessionId: 'sess-w', widgetToken: 'tok-1', enabled: false }),
    );
    await flush();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('sessionId:null NÃO dispara poll', async () => {
    fetchMock.mockResolvedValue(okWidgetResponse());
    renderHook(() => useWidgetPolling({ sessionId: null, widgetToken: 'tok-1' }));
    await flush();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('sem token e sem refreshToken: poll não chama fetch (sem credencial)', async () => {
    fetchMock.mockResolvedValue(okWidgetResponse());
    renderHook(() => useWidgetPolling({ sessionId: 'sess-w', widgetToken: null }));
    await flush();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('DESMONTAGEM cancela o timer: sem poll extra ao avançar o relógio', async () => {
    fetchMock.mockResolvedValue(okWidgetResponse());
    const { unmount } = renderHook(() =>
      useWidgetPolling({
        sessionId: 'sess-w',
        widgetToken: 'tok-1',
        widgetTokenExpiresAt: new Date(Date.now() + 10 * 60 * 1000).toISOString(),
      }),
    );
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    unmount();
    await act(async () => {
      vi.advanceTimersByTime(60 * 1000); // bem além do base interval
      await Promise.resolve();
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
