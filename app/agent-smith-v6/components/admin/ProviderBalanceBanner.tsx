'use client';

/**
 * ProviderBalanceBanner — red banner in the MASTER admin warning that a specific
 * LLM provider's PLATFORM account is out of balance/quota (so its agents are
 * failing). Platform-internal signal: rendered ONLY for the master admin and
 * never for tenants. Fed by GET /api/admin/provider-alerts (master-gated; returns
 * an empty list for anyone else). Auto-heals: the backend resolves the alert when
 * a turn for that provider succeeds again, so the banner disappears on its own
 * after a top-up (within one poll). Best-effort: any fetch error renders nothing.
 */

import { useEffect, useState } from 'react';
import { AlertTriangle } from 'lucide-react';

type ProviderAlert = {
  provider: string;
  kind: string;
  message: string | null;
  detected_at: string;
};

const PROVIDER_LABEL: Record<string, string> = {
  anthropic: 'Anthropic (Claude)',
  openai: 'OpenAI (GPT)',
  google: 'Google (Gemini)',
  openrouter: 'OpenRouter',
};

const POLL_MS = 60_000;

export function ProviderBalanceBanner({ enabled }: { enabled: boolean }) {
  const [alerts, setAlerts] = useState<ProviderAlert[]>([]);

  useEffect(() => {
    if (!enabled) {
      setAlerts([]);
      return;
    }
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch('/api/admin/provider-alerts', { credentials: 'include' });
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled) setAlerts(Array.isArray(data.alerts) ? data.alerts : []);
      } catch {
        /* silencioso — o banner é best-effort, nunca quebra o layout */
      }
    };
    void load();
    const id = setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [enabled]);

  if (!enabled || alerts.length === 0) return null;

  return (
    <div className="bg-danger/10 border-b border-danger/30 px-6 py-3 space-y-2">
      {alerts.map((a) => {
        const label = PROVIDER_LABEL[a.provider] || a.provider;
        return (
          <div key={a.provider} className="flex items-center gap-3">
            <AlertTriangle className="h-5 w-5 text-danger flex-shrink-0" />
            <div>
              <p className="text-danger font-semibold text-sm">Provedor sem saldo: {label}</p>
              <p className="text-muted-foreground text-xs">
                As respostas dos agentes que usam {label} estão falhando. Recarregue créditos na
                conta do provedor — este aviso some sozinho assim que voltar a funcionar.
              </p>
            </div>
          </div>
        );
      })}
    </div>
  );
}
