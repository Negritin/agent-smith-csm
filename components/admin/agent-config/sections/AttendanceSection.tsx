'use client';

/**
 * S10 — Configuração de Atendimento/handoff do AGENTE (SPEC §13/§3.3).
 *
 * Bloco com os controles de atendimento POR AGENTE (handoff humano, reabertura e
 * encerramento pelo agente), SALVO via o endpoint dedicado PATCH
 * /api/admin/agents/[agentId]/attendance-settings (S6), que faz DEEP-MERGE em
 * `agents.tools_config` (espelha SÓ human_handoff.enabled e end_attendance.enabled,
 * preservando csv_analytics e chaves desconhecidas — §9.3). NUNCA sobrescreve
 * `tools_config` inteiro.
 *
 * O encerramento automático por inatividade (auto_close_*) NÃO mora mais aqui —
 * virou config da EMPRESA (CompanyAttendanceSettings → /api/admin/company/
 * attendance-settings, §16). Esta seção mantém apenas as flags por-agente.
 *
 * `GET attendance-settings` retorna defaults mesmo sem registro (§9.3), então o
 * carregamento sempre tem valores válidos.
 */

import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { CheckCircle, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';

type AttendanceSettings = {
  handoff_enabled: boolean;
  reopen_on_customer_reply: boolean;
  agent_can_close: boolean;
};

const DEFAULTS: AttendanceSettings = {
  handoff_enabled: false,
  reopen_on_customer_reply: true,
  agent_can_close: false,
};

interface Props {
  agentId?: string;
  isSubagent: boolean;
  /**
   * Espelho do toggle de handoff humano que vive em PersonalitySection. Esta
   * seção é a fonte de verdade do handoff (salva em attendance-settings); ao
   * salvar aqui sincronizamos o estado do AgentConfigView para a UI ficar
   * coerente caso o usuário volte à aba Personalidade.
   */
  allowHumanHandoff: boolean;
  setAllowHumanHandoff: (value: boolean) => void;
  /**
   * Notifica o AgentConfigView após um PATCH attendance-settings bem-sucedido,
   * para que ele re-sincronize seu `loadedToolsConfigRef` (espelhos
   * human_handoff/end_attendance). Sem isso, um save GERAL posterior em outra aba
   * usaria um ref STALE e zeraria `end_attendance.enabled` recém-ligado aqui (§24).
   */
  onAttendanceSaved?: (mirrors: { handoffEnabled: boolean; agentCanClose: boolean }) => void;
}

export function AttendanceSection({
  agentId,
  isSubagent,
  allowHumanHandoff,
  setAllowHumanHandoff,
  onAttendanceSaved,
}: Props) {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [settings, setSettings] = useState<AttendanceSettings>({
    ...DEFAULTS,
    handoff_enabled: allowHumanHandoff,
  });

  useEffect(() => {
    if (!agentId) {
      setSettings({ ...DEFAULTS, handoff_enabled: allowHumanHandoff });
      return;
    }
    let cancelled = false;
    const load = async () => {
      setLoading(true);
      try {
        const res = await fetch(`/api/admin/agents/${agentId}/attendance-settings`, {
          credentials: 'include',
        });
        if (res.ok) {
          const data = await res.json();
          const s = (data.settings ?? {}) as Partial<AttendanceSettings>;
          if (!cancelled) {
            setSettings({
              // handoff_enabled é derivado da prop allowHumanHandoff (fonte única
              // do espelho, sincronizada via effect dedicado): NÃO sobrescrever
              // com o valor do servidor aqui evita descartar um toggle ainda não
              // salvo feito na aba Personalidade ao remontar esta seção (§22 item 2).
              handoff_enabled: allowHumanHandoff,
              reopen_on_customer_reply:
                s.reopen_on_customer_reply ?? DEFAULTS.reopen_on_customer_reply,
              agent_can_close: s.agent_can_close ?? DEFAULTS.agent_can_close,
            });
          }
        }
      } catch {
        if (!cancelled) toast.error('Erro ao carregar configurações de atendimento.');
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
    // Recarrega ao trocar de agente.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId]);

  // Mantém o toggle de handoff em sincronia com a prop allowHumanHandoff (fonte
  // exibida na aba Personalidade). Sem isto, um toggle de handoff feito na aba
  // Personalidade e ainda NÃO salvo seria descartado ao remontar esta seção, e
  // pior: o save de atendimento gravaria o valor STALE, revertendo a escolha do
  // usuário (§22 item 2 / §9.3). Sincronizar evita divergência entre as duas
  // fontes de escrita do espelho human_handoff.enabled.
  useEffect(() => {
    setSettings((prev) =>
      prev.handoff_enabled === allowHumanHandoff
        ? prev
        : { ...prev, handoff_enabled: allowHumanHandoff },
    );
  }, [allowHumanHandoff]);

  const update = (patch: Partial<AttendanceSettings>) =>
    setSettings((prev) => ({ ...prev, ...patch }));

  const handleSave = async () => {
    if (!agentId) {
      toast.warning('Salve o agente primeiro antes de configurar o atendimento.');
      return;
    }

    setSaving(true);
    try {
      const res = await fetch(`/api/admin/agents/${agentId}/attendance-settings`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          handoff_enabled: settings.handoff_enabled,
          reopen_on_customer_reply: settings.reopen_on_customer_reply,
          agent_can_close: settings.agent_can_close,
        }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || 'Falha ao salvar');
      }
      // Sincroniza o espelho de handoff exibido na aba Personalidade.
      setAllowHumanHandoff(settings.handoff_enabled);
      // Re-sincroniza o snapshot de tools_config do save GERAL (§24): impede que
      // um save posterior em outra aba zere os espelhos human_handoff/end_attendance
      // recém-gravados aqui via ref STALE.
      onAttendanceSaved?.({
        handoffEnabled: settings.handoff_enabled,
        agentCanClose: settings.agent_can_close,
      });
      toast.success('Configurações de atendimento salvas!');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Erro ao salvar atendimento.');
    } finally {
      setSaving(false);
    }
  };

  if (isSubagent) {
    return (
      <Card className="bg-card border-border">
        <CardContent className="py-8 text-center text-sm text-muted-foreground">
          Especialistas (subagentes) não têm atendimento humano nem auto-encerramento próprios.
        </CardContent>
      </Card>
    );
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12 text-muted-foreground">
        <Loader2 className="mr-2 h-6 w-6 animate-spin" /> Carregando...
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {!agentId && (
        <div className="rounded-md border border-border bg-muted/40 p-3 text-xs text-muted-foreground">
          Salve o agente primeiro para configurar o atendimento.
        </div>
      )}

      {/* Handoff humano: controlado SÓ na aba Personalidade ("Solicitar Humano",
          fonte única). Esta seção não exibe mais o toggle — apenas espelha e
          persiste handoff_enabled (settings sincronizado com a prop
          allowHumanHandoff) junto do save de atendimento, evitando o controle
          duplicado.

          O encerramento automático por inatividade (auto_close_*) foi movido para
          a config da EMPRESA (Configurações → Atendimento e SLA). */}

      {/* Reabertura + encerramento pelo agente */}
      <Card className="bg-card border-border">
        <CardHeader>
          <CardTitle className="text-sm text-foreground">Encerramento pelo agente</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Reabertura ao cliente responder: INVARIANTE GLOBAL (sempre liga, sem
              toggle). Inicia um novo atendimento na MESMA conversa, histórico
              contínuo. Controlado no backend (turn_runner_factory), não na UI. */}
          <div className="flex items-center justify-between">
            <div>
              <Label className="text-muted-foreground">
                Permitir que o agente encerre atendimentos
              </Label>
              <p className="text-xs text-muted-foreground">
                Habilita a ferramenta para a IA encerrar a conversa quando o assunto for resolvido.
              </p>
            </div>
            <Switch
              checked={settings.agent_can_close}
              onCheckedChange={(v) => update({ agent_can_close: v })}
            />
          </div>
        </CardContent>
      </Card>

      <div className="flex justify-end">
        <Button
          onClick={handleSave}
          disabled={saving || !agentId}
          className="bg-primary hover:bg-primary/90 text-primary-foreground"
        >
          {saving ? (
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          ) : (
            <CheckCircle className="mr-2 h-4 w-4" />
          )}
          Salvar atendimento
        </Button>
      </div>
    </div>
  );
}
