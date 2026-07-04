'use client';

/**
 * S10 — Configuração de SLA da EMPRESA (SPEC §14, §9.2/§9.4).
 *
 * Esta config é por `company_id` (NÃO por admin/operador): política ativa,
 * timezone, horário útil + dias úteis, prazos normal/high/critical (1ª resposta
 * + resolução), e destinatários de alerta (email/WhatsApp) com CRUD e teste de
 * envio. Consome:
 *   - GET/PUT  /api/admin/company/sla-policy        (política, §9.2)
 *   - GET/POST/PATCH/DELETE /api/admin/handoff-recipients (destinatários, §9.4)
 *   - POST     /api/admin/handoff-recipients/[id]/test    (teste de envio)
 *
 * master_admin precisa enviar `company_id` na query — derivado de `useAdminRole`.
 */

import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';
import {
  Building2,
  Clock,
  Loader2,
  Mail,
  MessageCircle,
  Plus,
  Save,
  Send,
  Trash2,
} from 'lucide-react';
import { useAdminRole } from '@/hooks/useAdminRole';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

// =========================================================================== //
// Tipos locais (espelham os contratos das rotas S6)
// =========================================================================== //

type SlaPolicy = {
  id?: string;
  company_id?: string;
  name?: string | null;
  is_active?: boolean;
  timezone?: string | null;
  business_hours_enabled?: boolean;
  working_days?: number[] | null;
  working_start?: string | null;
  working_end?: string | null;
  normal_first_response_minutes?: number | null;
  normal_resolution_minutes?: number | null;
  high_first_response_minutes?: number | null;
  high_resolution_minutes?: number | null;
  critical_first_response_minutes?: number | null;
  critical_resolution_minutes?: number | null;
  default_sla_level?: 'normal' | 'high' | 'critical' | null;
};

type Recipient = {
  id: string;
  channel: 'email' | 'whatsapp';
  recipient_value: string;
  display_name: string | null;
  enabled: boolean;
};

// Defaults coerentes com a SPEC (prazos em minutos; horário útil 09-18 seg-sex).
const POLICY_DEFAULTS: SlaPolicy = {
  name: 'Política padrão',
  is_active: true,
  timezone: 'America/Sao_Paulo',
  business_hours_enabled: true,
  working_days: [1, 2, 3, 4, 5],
  working_start: '09:00',
  working_end: '18:00',
  normal_first_response_minutes: 60,
  normal_resolution_minutes: 1440,
  high_first_response_minutes: 30,
  high_resolution_minutes: 480,
  critical_first_response_minutes: 15,
  critical_resolution_minutes: 240,
  default_sla_level: 'normal',
};

const WEEK_DAYS: { value: number; label: string }[] = [
  { value: 0, label: 'Dom' },
  { value: 1, label: 'Seg' },
  { value: 2, label: 'Ter' },
  { value: 3, label: 'Qua' },
  { value: 4, label: 'Qui' },
  { value: 5, label: 'Sex' },
  { value: 6, label: 'Sáb' },
];

const COMMON_TIMEZONES = [
  'America/Sao_Paulo',
  'America/Manaus',
  'America/Fortaleza',
  'America/Bahia',
  'UTC',
];

export function AttendanceSlaSettings() {
  const { companyId, role } = useAdminRole();
  const isMaster = role === 'master';

  const [loading, setLoading] = useState(true);
  const [savingPolicy, setSavingPolicy] = useState(false);
  const [policy, setPolicy] = useState<SlaPolicy>(POLICY_DEFAULTS);

  const [recipients, setRecipients] = useState<Recipient[]>([]);
  const [newChannel, setNewChannel] = useState<'email' | 'whatsapp'>('email');
  const [newValue, setNewValue] = useState('');
  const [newName, setNewName] = useState('');
  const [addingRecipient, setAddingRecipient] = useState(false);
  const [busyRecipientId, setBusyRecipientId] = useState<string | null>(null);

  // master_admin precisa do company_id na query (§9.2/§9.4).
  const companyQuery = isMaster && companyId ? `?company_id=${companyId}` : '';

  const loadAll = useCallback(async () => {
    setLoading(true);
    try {
      const [policyRes, recipientsRes] = await Promise.all([
        fetch(`/api/admin/company/sla-policy${companyQuery}`, { credentials: 'include' }),
        fetch(`/api/admin/handoff-recipients${companyQuery}`, { credentials: 'include' }),
      ]);

      if (policyRes.ok) {
        const data = await policyRes.json();
        // Sem política ativa => mantém defaults (handoff funciona sem SLA).
        setPolicy(data.policy ? { ...POLICY_DEFAULTS, ...data.policy } : POLICY_DEFAULTS);
      }
      if (recipientsRes.ok) {
        const data = await recipientsRes.json();
        setRecipients(Array.isArray(data.recipients) ? data.recipients : []);
      }
    } catch {
      toast.error('Erro ao carregar configurações de SLA.');
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
    void loadAll();
  }, [isMaster, companyId, loadAll]);

  const updatePolicy = (patch: Partial<SlaPolicy>) => setPolicy((prev) => ({ ...prev, ...patch }));

  const toggleWorkingDay = (day: number) => {
    const current = new Set(policy.working_days ?? []);
    if (current.has(day)) current.delete(day);
    else current.add(day);
    updatePolicy({ working_days: Array.from(current).sort((a, b) => a - b) });
  };

  const handleSavePolicy = async () => {
    // Validação client-side (§7.4): prazos inteiros >= 1 e, com horário útil,
    // início < fim e ao menos um dia útil. Bloqueia o save com toast em vez de
    // deixar o CHECK do banco estourar um 500 opaco.
    const minuteFields: Array<[keyof SlaPolicy, string]> = [
      ['normal_first_response_minutes', 'Normal · 1ª resposta'],
      ['normal_resolution_minutes', 'Normal · resolução'],
      ['high_first_response_minutes', 'Alta · 1ª resposta'],
      ['high_resolution_minutes', 'Alta · resolução'],
      ['critical_first_response_minutes', 'Crítica · 1ª resposta'],
      ['critical_resolution_minutes', 'Crítica · resolução'],
    ];
    for (const [key, label] of minuteFields) {
      const value = Number(policy[key]);
      if (!Number.isInteger(value) || value < 1) {
        toast.error(`O prazo "${label}" deve ser de no mínimo 1 minuto.`);
        return;
      }
    }
    if (policy.business_hours_enabled) {
      const start = (policy.working_start ?? '').slice(0, 5);
      const end = (policy.working_end ?? '').slice(0, 5);
      if (start && end && start >= end) {
        toast.error('O horário de início deve ser anterior ao de fim.');
        return;
      }
      if (!(policy.working_days ?? []).length) {
        toast.error('Selecione ao menos um dia útil.');
        return;
      }
    }

    setSavingPolicy(true);
    try {
      const res = await fetch(`/api/admin/company/sla-policy${companyQuery}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          name: policy.name,
          timezone: policy.timezone,
          business_hours_enabled: policy.business_hours_enabled,
          working_days: policy.working_days,
          working_start: policy.working_start,
          working_end: policy.working_end,
          normal_first_response_minutes: policy.normal_first_response_minutes,
          normal_resolution_minutes: policy.normal_resolution_minutes,
          high_first_response_minutes: policy.high_first_response_minutes,
          high_resolution_minutes: policy.high_resolution_minutes,
          critical_first_response_minutes: policy.critical_first_response_minutes,
          critical_resolution_minutes: policy.critical_resolution_minutes,
          default_sla_level: policy.default_sla_level,
        }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || 'Falha ao salvar');
      }
      const data = await res.json();
      if (data.policy) setPolicy({ ...POLICY_DEFAULTS, ...data.policy });
      toast.success('Política de SLA salva!');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Erro ao salvar política.');
    } finally {
      setSavingPolicy(false);
    }
  };

  const handleAddRecipient = async () => {
    if (!newValue.trim()) {
      toast.warning('Informe o e-mail ou telefone do destinatário.');
      return;
    }
    setAddingRecipient(true);
    try {
      const res = await fetch(`/api/admin/handoff-recipients${companyQuery}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          channel: newChannel,
          recipient_value: newValue.trim(),
          display_name: newName.trim() || null,
        }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || 'Falha ao adicionar');
      }
      const data = await res.json();
      if (data.recipient) {
        setRecipients((prev) => {
          const without = prev.filter((r) => r.id !== data.recipient.id);
          return [data.recipient, ...without];
        });
      }
      setNewValue('');
      setNewName('');
      toast.success('Destinatário adicionado!');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Erro ao adicionar destinatário.');
    } finally {
      setAddingRecipient(false);
    }
  };

  const handleToggleRecipient = async (recipient: Recipient) => {
    setBusyRecipientId(recipient.id);
    try {
      const res = await fetch(`/api/admin/handoff-recipients/${recipient.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ enabled: !recipient.enabled }),
      });
      if (!res.ok) throw new Error('Falha ao atualizar');
      setRecipients((prev) =>
        prev.map((r) => (r.id === recipient.id ? { ...r, enabled: !r.enabled } : r)),
      );
    } catch {
      toast.error('Erro ao atualizar destinatário.');
    } finally {
      setBusyRecipientId(null);
    }
  };

  const handleDeleteRecipient = async (id: string) => {
    setBusyRecipientId(id);
    try {
      const res = await fetch(`/api/admin/handoff-recipients/${id}`, {
        method: 'DELETE',
        credentials: 'include',
      });
      if (!res.ok) throw new Error('Falha ao remover');
      setRecipients((prev) => prev.filter((r) => r.id !== id));
      toast.success('Destinatário removido.');
    } catch {
      toast.error('Erro ao remover destinatário.');
    } finally {
      setBusyRecipientId(null);
    }
  };

  const handleTestRecipient = async (id: string) => {
    setBusyRecipientId(id);
    try {
      const res = await fetch(`/api/admin/handoff-recipients/${id}/test`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({}),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || 'Falha no teste');
      }
      toast.success('Teste enfileirado; será entregue pelo worker de notificações.');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Erro ao testar envio.');
    } finally {
      setBusyRecipientId(null);
    }
  };

  if (isMaster && !companyId) {
    return (
      <Card className="bg-card border-border">
        <CardContent className="py-8 text-center text-sm text-muted-foreground">
          Selecione uma empresa para configurar o SLA.
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
      {/* Aviso: por EMPRESA, não por admin */}
      <div className="flex items-start gap-2 rounded-md border border-border bg-muted/40 p-3 text-xs text-muted-foreground">
        <Building2 className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
        <p>
          Esta configuração vale para a <span className="font-semibold">empresa inteira</span> — não
          é por administrador nem por operador. Todos os agentes e atendimentos da empresa usam esta
          política de SLA e estes destinatários de alerta.
        </p>
      </div>

      {/* ===== Política de SLA ===== */}
      <Card className="bg-card border-border">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-foreground">
            <Clock className="h-4 w-4" /> Política de SLA
          </CardTitle>
          <CardDescription>Prazos e horário de atendimento da empresa.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <div className="space-y-2">
              <Label className="text-foreground">Nome da política</Label>
              <Input
                value={policy.name ?? ''}
                onChange={(e) => updatePolicy({ name: e.target.value })}
                className="bg-background border-input text-foreground"
              />
            </div>
            <div className="space-y-2">
              <Label className="text-foreground">Fuso horário</Label>
              <Select
                value={policy.timezone ?? 'America/Sao_Paulo'}
                onValueChange={(v) => updatePolicy({ timezone: v })}
              >
                <SelectTrigger className="bg-background border-border text-foreground">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent className="bg-card border-border">
                  {COMMON_TIMEZONES.map((tz) => (
                    <SelectItem key={tz} value={tz} className="text-foreground hover:bg-muted">
                      {tz}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          {/* Horário útil */}
          <div className="border-t border-border pt-4">
            <div className="flex items-center justify-between">
              <div>
                <Label className="text-foreground">Horário útil</Label>
                <p className="text-xs text-muted-foreground">
                  Quando desligado, o SLA conta 24/7. Ligado, conta só nas horas/dias úteis.
                </p>
              </div>
              <Switch
                checked={!!policy.business_hours_enabled}
                onCheckedChange={(v) => updatePolicy({ business_hours_enabled: v })}
              />
            </div>

            {policy.business_hours_enabled && (
              <div className="mt-4 space-y-4">
                <div className="grid grid-cols-2 gap-4 md:max-w-sm">
                  <div className="space-y-2">
                    <Label className="text-foreground">Início</Label>
                    <Input
                      type="time"
                      value={(policy.working_start ?? '09:00').slice(0, 5)}
                      onChange={(e) => updatePolicy({ working_start: e.target.value })}
                      className="bg-background border-input text-foreground"
                    />
                  </div>
                  <div className="space-y-2">
                    <Label className="text-foreground">Fim</Label>
                    <Input
                      type="time"
                      value={(policy.working_end ?? '18:00').slice(0, 5)}
                      onChange={(e) => updatePolicy({ working_end: e.target.value })}
                      className="bg-background border-input text-foreground"
                    />
                  </div>
                </div>
                <div className="space-y-2">
                  <Label className="text-foreground">Dias úteis</Label>
                  <div className="flex flex-wrap gap-2">
                    {WEEK_DAYS.map((d) => {
                      const active = (policy.working_days ?? []).includes(d.value);
                      return (
                        <button
                          key={d.value}
                          type="button"
                          onClick={() => toggleWorkingDay(d.value)}
                          className={`h-8 w-12 rounded-md border text-xs font-medium transition-colors ${
                            active
                              ? 'border-primary bg-primary text-primary-foreground'
                              : 'border-border bg-background text-muted-foreground hover:bg-muted'
                          }`}
                        >
                          {d.label}
                        </button>
                      );
                    })}
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* Prazos por nível */}
          <div className="border-t border-border pt-4">
            <Label className="text-foreground">Prazos (em minutos)</Label>
            <p className="mb-3 text-xs text-muted-foreground">
              Primeira resposta e resolução por nível de prioridade.
            </p>
            <div className="space-y-3">
              {(
                [
                  {
                    level: 'normal',
                    label: 'Normal',
                    fr: 'normal_first_response_minutes',
                    rs: 'normal_resolution_minutes',
                  },
                  {
                    level: 'high',
                    label: 'Alta',
                    fr: 'high_first_response_minutes',
                    rs: 'high_resolution_minutes',
                  },
                  {
                    level: 'critical',
                    label: 'Crítica',
                    fr: 'critical_first_response_minutes',
                    rs: 'critical_resolution_minutes',
                  },
                ] as const
              ).map((row) => (
                <div
                  key={row.level}
                  className="grid grid-cols-1 items-end gap-3 rounded-md border border-border bg-background/40 p-3 sm:grid-cols-[80px_1fr_1fr]"
                >
                  <span className="text-sm font-medium text-foreground">{row.label}</span>
                  <div className="space-y-1">
                    <Label className="text-[11px] text-muted-foreground">1ª resposta</Label>
                    <Input
                      type="number"
                      min={1}
                      value={Number(policy[row.fr] ?? 0)}
                      onChange={(e) =>
                        updatePolicy({
                          [row.fr]: parseInt(e.target.value, 10) || 0,
                        } as Partial<SlaPolicy>)
                      }
                      className="bg-background border-input text-foreground"
                    />
                  </div>
                  <div className="space-y-1">
                    <Label className="text-[11px] text-muted-foreground">Resolução</Label>
                    <Input
                      type="number"
                      min={1}
                      value={Number(policy[row.rs] ?? 0)}
                      onChange={(e) =>
                        updatePolicy({
                          [row.rs]: parseInt(e.target.value, 10) || 0,
                        } as Partial<SlaPolicy>)
                      }
                      className="bg-background border-input text-foreground"
                    />
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="flex justify-end border-t border-border pt-4">
            <Button
              onClick={handleSavePolicy}
              disabled={savingPolicy}
              className="bg-primary hover:bg-primary/90 text-primary-foreground"
            >
              {savingPolicy ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              ) : (
                <Save className="mr-2 h-4 w-4" />
              )}
              Salvar política
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* ===== Destinatários ===== */}
      <Card className="bg-card border-border">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-foreground">
            <Send className="h-4 w-4" /> Destinatários de alerta
          </CardTitle>
          <CardDescription>
            Quem recebe o alerta quando uma conversa solicita atendimento humano.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Adicionar */}
          <div className="grid grid-cols-1 items-end gap-3 sm:grid-cols-[140px_1fr_1fr_auto]">
            <div className="space-y-1">
              <Label className="text-[11px] text-muted-foreground">Canal</Label>
              <Select
                value={newChannel}
                onValueChange={(v) => setNewChannel(v as 'email' | 'whatsapp')}
              >
                <SelectTrigger className="bg-background border-border text-foreground">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent className="bg-card border-border">
                  <SelectItem value="email" className="text-foreground hover:bg-muted">
                    E-mail
                  </SelectItem>
                  <SelectItem value="whatsapp" className="text-foreground hover:bg-muted">
                    WhatsApp
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label className="text-[11px] text-muted-foreground">
                {newChannel === 'email' ? 'E-mail' : 'Telefone'}
              </Label>
              <Input
                value={newValue}
                onChange={(e) => setNewValue(e.target.value)}
                placeholder={newChannel === 'email' ? 'nome@empresa.com' : '+55 11 99999-9999'}
                className="bg-background border-input text-foreground"
              />
            </div>
            <div className="space-y-1">
              <Label className="text-[11px] text-muted-foreground">Nome (opcional)</Label>
              <Input
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                placeholder="Equipe de suporte"
                className="bg-background border-input text-foreground"
              />
            </div>
            <Button
              onClick={handleAddRecipient}
              disabled={addingRecipient}
              className="bg-primary hover:bg-primary/90 text-primary-foreground"
            >
              {addingRecipient ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Plus className="h-4 w-4" />
              )}
              <span className="ml-1">Adicionar</span>
            </Button>
          </div>

          {/* Lista */}
          {recipients.length === 0 ? (
            <p className="py-4 text-center text-sm text-muted-foreground">
              Nenhum destinatário cadastrado.
            </p>
          ) : (
            <ul className="space-y-2">
              {recipients.map((r) => (
                <li
                  key={r.id}
                  className="flex flex-wrap items-center gap-3 rounded-md border border-border bg-background/40 p-3"
                >
                  {r.channel === 'email' ? (
                    <Mail className="h-4 w-4 shrink-0 text-primary" />
                  ) : (
                    <MessageCircle className="h-4 w-4 shrink-0 text-success" />
                  )}
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-foreground">
                      {r.recipient_value}
                    </p>
                    {r.display_name && (
                      <p className="truncate text-xs text-muted-foreground">{r.display_name}</p>
                    )}
                  </div>
                  <Badge
                    className={`h-5 px-1.5 text-[9px] uppercase ${
                      r.enabled
                        ? 'border-success/20 bg-success/10 text-success'
                        : 'border-border bg-muted text-muted-foreground'
                    }`}
                  >
                    {r.enabled ? 'Ativo' : 'Inativo'}
                  </Badge>
                  <div className="flex items-center gap-1">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-8 px-2 text-xs"
                      disabled={busyRecipientId === r.id || !r.enabled}
                      onClick={() => handleTestRecipient(r.id)}
                      title="Enviar teste"
                    >
                      <Send className="h-3.5 w-3.5" />
                    </Button>
                    <Switch
                      checked={r.enabled}
                      disabled={busyRecipientId === r.id}
                      onCheckedChange={() => handleToggleRecipient(r)}
                    />
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-8 px-2 text-danger hover:text-danger"
                      disabled={busyRecipientId === r.id}
                      onClick={() => handleDeleteRecipient(r.id)}
                      title="Remover"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
