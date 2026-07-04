'use client';

/**
 * Encerramento automático por inatividade da EMPRESA (SPEC §13/§16).
 *
 * Esta config é por `company_id` (NÃO por agente nem por admin): vive em
 * `company_attendance_settings` e é lida pelo worker de inatividade (§16) e pelo
 * InactivityTimerService. Consome:
 *   - GET/PUT /api/admin/company/attendance-settings
 *
 * master_admin precisa enviar `company_id` na query — derivado de `useAdminRole`.
 * Espelha a estrutura/auth de AttendanceSlaSettings (config company-level).
 */

import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';
import { Building2, Loader2, Save, Timer } from 'lucide-react';
import { useAdminRole } from '@/hooks/useAdminRole';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

type AutoCloseScope = 'all_attendance' | 'human_only';

type CompanyAttendanceSettings = {
  auto_close_enabled: boolean;
  auto_close_after_minutes: number;
  auto_close_scope: AutoCloseScope;
  auto_close_message_enabled: boolean;
  auto_close_message: string;
};

// Defaults idênticos à migration 20260628_01 / rota company (GET sem registro).
const DEFAULTS: CompanyAttendanceSettings = {
  auto_close_enabled: false,
  auto_close_after_minutes: 240,
  auto_close_scope: 'all_attendance',
  auto_close_message_enabled: true,
  auto_close_message:
    'Encerramos este atendimento por falta de resposta. Se precisar, é só chamar novamente.',
};

const MIN_MINUTES = 5;

export function CompanyAttendanceSettings() {
  const { companyId, role } = useAdminRole();
  const isMaster = role === 'master';

  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  // Unidade exibida ao usuário; o backend sempre recebe MINUTOS.
  const [unit, setUnit] = useState<'minutes' | 'hours'>('minutes');
  const [settings, setSettings] = useState<CompanyAttendanceSettings>(DEFAULTS);

  // master_admin precisa do company_id na query (§16).
  const companyQuery = isMaster && companyId ? `?company_id=${companyId}` : '';

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`/api/admin/company/attendance-settings${companyQuery}`, {
        credentials: 'include',
      });
      if (res.ok) {
        const data = await res.json();
        const s = (data.settings ?? {}) as Partial<CompanyAttendanceSettings>;
        const minutes = Number(s.auto_close_after_minutes ?? DEFAULTS.auto_close_after_minutes);
        setSettings({
          auto_close_enabled: s.auto_close_enabled ?? DEFAULTS.auto_close_enabled,
          auto_close_after_minutes: minutes,
          auto_close_scope: (s.auto_close_scope as AutoCloseScope) ?? DEFAULTS.auto_close_scope,
          auto_close_message_enabled:
            s.auto_close_message_enabled ?? DEFAULTS.auto_close_message_enabled,
          auto_close_message: s.auto_close_message ?? DEFAULTS.auto_close_message,
        });
        // Exibe em horas quando o valor é múltiplo exato de 60 (>= 1h).
        if (minutes >= 60 && minutes % 60 === 0) setUnit('hours');
      }
    } catch {
      toast.error('Erro ao carregar configurações de encerramento.');
    } finally {
      setLoading(false);
    }
  }, [companyQuery]);

  useEffect(() => {
    // master sem company selecionada não tem o que carregar.
    if (isMaster && !companyId) {
      setLoading(false);
      return;
    }
    void load();
  }, [isMaster, companyId, load]);

  const update = (patch: Partial<CompanyAttendanceSettings>) =>
    setSettings((prev) => ({ ...prev, ...patch }));

  // Valor exibido no input conforme a unidade selecionada.
  const displayedAfter =
    unit === 'hours'
      ? Math.round((settings.auto_close_after_minutes / 60) * 100) / 100
      : settings.auto_close_after_minutes;
  const minDisplayed = unit === 'hours' ? MIN_MINUTES / 60 : MIN_MINUTES;

  const handleAfterChange = (raw: string) => {
    const value = parseFloat(raw);
    if (Number.isNaN(value)) return;
    const minutes = unit === 'hours' ? Math.round(value * 60) : Math.round(value);
    update({ auto_close_after_minutes: minutes });
  };

  const handleSave = async () => {
    // Validação: minutos >= 5.
    if (settings.auto_close_after_minutes < MIN_MINUTES) {
      toast.error('O tempo para encerrar deve ser de no mínimo 5 minutos.');
      return;
    }
    // Validação: mensagem final obrigatória quando habilitada (§13/§16).
    if (settings.auto_close_message_enabled && !settings.auto_close_message.trim()) {
      toast.error('A mensagem final é obrigatória quando o envio está habilitado.');
      return;
    }

    setSaving(true);
    try {
      const res = await fetch(`/api/admin/company/attendance-settings${companyQuery}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          auto_close_enabled: settings.auto_close_enabled,
          auto_close_after_minutes: settings.auto_close_after_minutes,
          auto_close_scope: settings.auto_close_scope,
          auto_close_message_enabled: settings.auto_close_message_enabled,
          auto_close_message: settings.auto_close_message,
        }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || 'Falha ao salvar');
      }
      const data = await res.json();
      if (data.settings) setSettings({ ...DEFAULTS, ...data.settings });
      toast.success('Configurações de encerramento salvas!');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Erro ao salvar encerramento.');
    } finally {
      setSaving(false);
    }
  };

  if (isMaster && !companyId) {
    return (
      <Card className="bg-card border-border">
        <CardContent className="py-8 text-center text-sm text-muted-foreground">
          Selecione uma empresa para configurar o encerramento automático.
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
      {/* Aviso: por EMPRESA, não por agente */}
      <div className="flex items-start gap-2 rounded-md border border-border bg-muted/40 p-3 text-xs text-muted-foreground">
        <Building2 className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
        <p>
          Esta configuração vale para a <span className="font-semibold">empresa inteira</span> — não
          é por agente nem por administrador. Todos os atendimentos da empresa usam estas regras de
          encerramento automático por inatividade.
        </p>
      </div>

      <Card className="bg-card border-border">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-foreground">
            <Timer className="h-4 w-4" /> Encerramento automático por inatividade
          </CardTitle>
          <CardDescription>
            Encerra a conversa após um período sem resposta do cliente.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <Label className="text-muted-foreground">
                Encerrar automaticamente por inatividade
              </Label>
              <p className="text-xs text-muted-foreground">
                Encerra a conversa após um período sem resposta.
              </p>
            </div>
            <Switch
              checked={settings.auto_close_enabled}
              onCheckedChange={(v) => update({ auto_close_enabled: v })}
            />
          </div>

          {settings.auto_close_enabled && (
            <div className="space-y-4 border-t border-border pt-4">
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-[1fr_140px]">
                <div className="space-y-1">
                  <Label className="text-muted-foreground">Encerrar após</Label>
                  <Input
                    type="number"
                    min={minDisplayed}
                    step={unit === 'hours' ? 0.5 : 1}
                    value={displayedAfter}
                    onChange={(e) => handleAfterChange(e.target.value)}
                    className="bg-background border-input text-foreground"
                  />
                  <p className="text-[11px] text-muted-foreground">Mínimo 5 minutos.</p>
                </div>
                <div className="space-y-1">
                  <Label className="text-muted-foreground">Unidade</Label>
                  <Select value={unit} onValueChange={(v) => setUnit(v as 'minutes' | 'hours')}>
                    <SelectTrigger className="bg-background border-border text-foreground">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent className="bg-card border-border">
                      <SelectItem value="minutes" className="text-foreground hover:bg-muted">
                        Minutos
                      </SelectItem>
                      <SelectItem value="hours" className="text-foreground hover:bg-muted">
                        Horas
                      </SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>

              <div className="space-y-1">
                <Label className="text-muted-foreground">Escopo</Label>
                <Select
                  value={settings.auto_close_scope}
                  onValueChange={(v) => update({ auto_close_scope: v as AutoCloseScope })}
                >
                  <SelectTrigger className="bg-background border-border text-foreground">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent className="bg-card border-border">
                    <SelectItem value="all_attendance" className="text-foreground hover:bg-muted">
                      Todo atendimento (IA e humano)
                    </SelectItem>
                    <SelectItem value="human_only" className="text-foreground hover:bg-muted">
                      Apenas atendimento humano
                    </SelectItem>
                  </SelectContent>
                </Select>
              </div>

              <div className="flex items-center justify-between border-t border-border pt-4">
                <div>
                  <Label className="text-muted-foreground">Enviar mensagem final</Label>
                  <p className="text-xs text-muted-foreground">
                    Envia uma mensagem ao cliente ao encerrar por inatividade.
                  </p>
                </div>
                <Switch
                  checked={settings.auto_close_message_enabled}
                  onCheckedChange={(v) => update({ auto_close_message_enabled: v })}
                />
              </div>

              {settings.auto_close_message_enabled && (
                <div className="space-y-1">
                  <Label className="text-muted-foreground">
                    Mensagem final <span className="text-danger">*</span>
                  </Label>
                  <Textarea
                    value={settings.auto_close_message}
                    onChange={(e) => update({ auto_close_message: e.target.value })}
                    rows={3}
                    placeholder="Encerramos este atendimento por falta de resposta..."
                    className="bg-background border-border text-foreground resize-y"
                  />
                  <p className="text-[11px] text-muted-foreground">
                    Obrigatória quando o envio está habilitado.
                  </p>
                </div>
              )}
            </div>
          )}

          <div className="flex justify-end border-t border-border pt-4">
            <Button
              onClick={handleSave}
              disabled={saving}
              className="bg-primary hover:bg-primary/90 text-primary-foreground"
            >
              {saving ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              ) : (
                <Save className="mr-2 h-4 w-4" />
              )}
              Salvar encerramento
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
