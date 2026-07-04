'use client';

import { useState, useEffect } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Switch } from '@/components/ui/switch';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { useToast } from '@/hooks/use-toast';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Bot,
  Plus,
  Trash2,
  Loader2,
  Users,
  Settings2,
  AlertTriangle,
  Clock,
  RotateCw,
  FileText,
  Edit2,
  Check,
  X,
} from 'lucide-react';

interface Delegation {
  id: string;
  orchestrator_id: string;
  subagent_id: string;
  task_description: string;
  is_active: boolean;
  max_context_chars: number;
  timeout_seconds: number;
  max_iterations: number;
  subagent_name?: string;
  subagent_avatar_url?: string;
}

interface AvailableAgent {
  id: string;
  name: string;
  is_subagent?: boolean;
}

interface SubAgentConfigTabProps {
  agentId: string;
  companyId: string;
}

// ─── Individual Delegation Card with Toggle & Edit ───────────────────────────
function DelegationCard({
  delegation: d,
  backendUrl,
  onUpdate,
  onDelete,
  toast,
}: {
  delegation: Delegation;
  backendUrl: string;
  onUpdate: () => Promise<void>;
  onDelete: () => void;
  toast: ReturnType<typeof useToast>['toast'];
}) {
  const [isEditing, setIsEditing] = useState(false);
  const [editDesc, setEditDesc] = useState(d.task_description);
  const [editTimeout, setEditTimeout] = useState(d.timeout_seconds);
  const [editIterations, setEditIterations] = useState(d.max_iterations);
  const [editContext, setEditContext] = useState(d.max_context_chars);
  const [savingEdit, setSavingEdit] = useState(false);
  const [toggling, setToggling] = useState(false);

  const handleToggleActive = async (newValue: boolean) => {
    setToggling(true);
    try {
      const res = await fetch(`/api/admin/proxy/agents/delegations/${d.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_active: newValue }),
      });
      if (!res.ok) {
        throw new Error('Falha ao atualizar');
      }
      toast({
        title: newValue ? 'Ativado' : 'Desativado',
        description: `${d.subagent_name || 'SubAgent'} ${newValue ? 'ativado' : 'desativado'} com sucesso.`,
      });
      await onUpdate();
    } catch (error) {
      console.error('Erro ao atualizar especialista:', error);
      toast({
        title: 'Erro',
        description: 'Não foi possível atualizar o especialista.',
        variant: 'destructive',
      });
    } finally {
      setToggling(false);
    }
  };

  const handleSaveEdit = async () => {
    if (!editDesc.trim() || editDesc.trim().length < 5) {
      toast({
        title: 'Atenção',
        description: 'Descrição deve ter pelo menos 5 caracteres.',
        variant: 'destructive',
      });
      return;
    }
    setSavingEdit(true);
    try {
      const res = await fetch(`/api/admin/proxy/agents/delegations/${d.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          task_description: editDesc.trim(),
          timeout_seconds: editTimeout,
          max_iterations: editIterations,
          max_context_chars: editContext,
        }),
      });
      if (!res.ok) {
        throw new Error('Falha ao salvar');
      }
      toast({ title: 'Salvo', description: 'Configuração do especialista atualizada.' });
      setIsEditing(false);
      await onUpdate();
    } catch (error) {
      console.error('Erro ao salvar especialista:', error);
      toast({
        title: 'Erro',
        description: 'Não foi possível salvar o especialista.',
        variant: 'destructive',
      });
    } finally {
      setSavingEdit(false);
    }
  };

  const handleCancelEdit = () => {
    setEditDesc(d.task_description);
    setEditTimeout(d.timeout_seconds);
    setEditIterations(d.max_iterations);
    setEditContext(d.max_context_chars);
    setIsEditing(false);
  };

  return (
    <div
      className={`flex flex-col gap-2 p-3 rounded-lg bg-muted/50 border transition-colors ${
        d.is_active ? 'border-border hover:border-primary/20' : 'border-border/50 opacity-60'
      }`}
    >
      {/* Top row: name, badge, actions */}
      <div className="flex items-start gap-3">
        <div className="w-8 h-8 rounded-full bg-brand-muted flex items-center justify-center flex-shrink-0 mt-0.5">
          <Bot className="w-4 h-4 text-primary " />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-foreground">
              {d.subagent_name || 'SubAgent'}
            </span>
            <Badge
              className={`text-[10px] px-1.5 py-0 ${
                d.is_active
                  ? 'bg-success/10 text-success border-success/20'
                  : 'bg-muted text-muted-foreground border-border'
              }`}
            >
              {d.is_active ? 'Ativo' : 'Inativo'}
            </Badge>
          </div>

          {!isEditing && (
            <>
              <p className="text-xs text-muted-foreground mt-1">{d.task_description}</p>
              <div className="flex gap-3 mt-2 text-[10px] text-muted-foreground">
                <span className="flex items-center gap-1">
                  <Clock className="w-3 h-3 text-primary " /> {d.timeout_seconds}s
                </span>
                <span className="flex items-center gap-1">
                  <RotateCw className="w-3 h-3 text-primary " /> {d.max_iterations} iter
                </span>
                <span className="flex items-center gap-1">
                  <FileText className="w-3 h-3 text-primary " /> {d.max_context_chars} chars
                </span>
              </div>
            </>
          )}
        </div>

        {/* Action buttons */}
        <div className="flex items-center gap-2 flex-shrink-0">
          <Switch checked={d.is_active} onCheckedChange={handleToggleActive} disabled={toggling} />
          {!isEditing && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setIsEditing(true)}
              className="text-muted-foreground hover:text-foreground hover:bg-brand-muted h-8 w-8 p-0"
              title="Editar"
            >
              <Edit2 className="w-3.5 h-3.5" />
            </Button>
          )}
          <Button
            variant="ghost"
            size="sm"
            onClick={onDelete}
            className="text-danger hover:text-danger hover:bg-danger/10 h-8 w-8 p-0"
            title="Remover"
          >
            <Trash2 className="w-3.5 h-3.5" />
          </Button>
        </div>
      </div>

      {/* Inline edit form */}
      {isEditing && (
        <div className="mt-2 space-y-3 pl-11 border-t border-border/50 pt-3">
          <div>
            <Label className="text-muted-foreground text-xs">Descrição da Tarefa</Label>
            <Textarea
              value={editDesc}
              onChange={(e) => setEditDesc(e.target.value)}
              className="bg-background border-border text-foreground text-xs h-20 mt-1"
            />
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div>
              <Label className="text-muted-foreground text-[10px]">Timeout (s)</Label>
              <Input
                type="number"
                min={5}
                max={120}
                value={editTimeout}
                onChange={(e) => setEditTimeout(Number(e.target.value))}
                className="bg-background border-border text-foreground text-xs mt-1"
              />
            </div>
            <div>
              <Label className="text-muted-foreground text-[10px]">Max Iterações</Label>
              <Input
                type="number"
                min={1}
                max={15}
                value={editIterations}
                onChange={(e) => setEditIterations(Number(e.target.value))}
                className="bg-background border-border text-foreground text-xs mt-1"
              />
            </div>
            <div>
              <Label className="text-muted-foreground text-[10px]">Contexto (chars)</Label>
              <Input
                type="number"
                min={500}
                max={10000}
                step={500}
                value={editContext}
                onChange={(e) => setEditContext(Number(e.target.value))}
                className="bg-background border-border text-foreground text-xs mt-1"
              />
            </div>
          </div>
          <div className="flex gap-2">
            <Button
              size="sm"
              onClick={handleSaveEdit}
              disabled={savingEdit || !editDesc.trim()}
              className="bg-primary hover:bg-primary/90 text-primary-foreground text-xs"
            >
              {savingEdit ? (
                <Loader2 className="w-3 h-3 mr-1 animate-spin" />
              ) : (
                <Check className="w-3 h-3 mr-1" />
              )}
              Salvar
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={handleCancelEdit}
              className="text-muted-foreground border-border text-xs"
            >
              <X className="w-3 h-3 mr-1" />
              Cancelar
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Main Component ──────────────────────────────────────────────────────────
export function SubAgentConfigTab({ agentId, companyId }: SubAgentConfigTabProps) {
  const [delegations, setDelegations] = useState<Delegation[]>([]);
  const [availableAgents, setAvailableAgents] = useState<AvailableAgent[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  // Form
  const [showForm, setShowForm] = useState(false);
  const [selectedSubagentId, setSelectedSubagentId] = useState('');
  const [taskDescription, setTaskDescription] = useState('');
  const [maxContextChars, setMaxContextChars] = useState(2000);
  const [timeoutSeconds, setTimeoutSeconds] = useState(30);
  const [maxIterations, setMaxIterations] = useState(5);
  const [deleteDelegationId, setDeleteDelegationId] = useState<string | null>(null);

  const { toast } = useToast();
  const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL || 'http://localhost:8000';

  useEffect(() => {
    if (agentId) {
      loadDelegations();
      loadAvailableAgents();
    }
  }, [agentId]);

  const loadDelegations = async () => {
    setLoading(true);
    try {
      const res = await fetch(`/api/admin/proxy/agents/${agentId}/delegations`);
      if (res.ok) {
        const data = await res.json();
        setDelegations(data);
      }
    } catch (e) {
      console.error('Erro ao carregar delegações:', e);
    } finally {
      setLoading(false);
    }
  };

  const loadAvailableAgents = async () => {
    try {
      const res = await fetch(`/api/admin/proxy/agents/company/${companyId}`);
      if (res.ok) {
        const agents: AvailableAgent[] = await res.json();
        setAvailableAgents(agents.filter((a) => a.id !== agentId && a.is_subagent));
      }
    } catch (e) {
      console.error('Erro ao carregar agentes:', e);
    }
  };

  const handleCreate = async () => {
    if (!selectedSubagentId || !taskDescription.trim()) {
      toast({
        title: 'Atenção',
        description: 'Selecione um agente e descreva a tarefa.',
        variant: 'destructive',
      });
      return;
    }

    setSaving(true);
    try {
      const res = await fetch(`/api/admin/proxy/agents/delegations`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          orchestrator_id: agentId,
          subagent_id: selectedSubagentId,
          task_description: taskDescription.trim(),
          max_context_chars: maxContextChars,
          timeout_seconds: timeoutSeconds,
          max_iterations: maxIterations,
        }),
      });

      if (!res.ok) {
        throw new Error('Falha ao criar delegação');
      }

      toast({ title: 'Sucesso', description: 'Especialista vinculado!' });
      setShowForm(false);
      resetForm();
      await loadDelegations();
      await loadAvailableAgents();
    } catch (error) {
      console.error('Erro ao criar delegação:', error);
      toast({
        title: 'Erro',
        description: 'Não foi possível vincular o especialista.',
        variant: 'destructive',
      });
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (delegationId: string) => {
    try {
      const res = await fetch(`/api/admin/proxy/agents/delegations/${delegationId}`, {
        method: 'DELETE',
      });
      if (!res.ok) {
        throw new Error('Falha ao remover');
      }
      toast({ title: 'Removido', description: 'Especialista desvinculado' });
      await loadDelegations();
      await loadAvailableAgents();
    } catch (e) {
      toast({
        title: 'Erro',
        description: 'Falha ao remover',
        variant: 'destructive',
      });
    }
  };

  const resetForm = () => {
    setSelectedSubagentId('');
    setTaskDescription('');
    setMaxContextChars(2000);
    setTimeoutSeconds(30);
    setMaxIterations(5);
  };

  const delegatedIds = new Set(delegations.map((d) => d.subagent_id));
  const filteredAgents = availableAgents.filter((a) => !delegatedIds.has(a.id));

  return (
    <div className="space-y-6">
      {/* Header */}
      <Card className="bg-card border-border">
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between">
            <CardTitle className="text-sm text-foreground flex items-center gap-2">
              <Users className="w-4 h-4 text-primary " />
              Especialistas (SubAgents)
            </CardTitle>
            {!showForm && (
              <Button
                size="sm"
                onClick={() => setShowForm(true)}
                className="bg-primary hover:bg-primary/90 text-primary-foreground text-xs"
                disabled={filteredAgents.length === 0}
              >
                <Plus className="w-3 h-3 mr-1" />
                Vincular Especialista
              </Button>
            )}
          </div>
          <p className="text-xs text-muted-foreground mt-1">
            Configure agentes especialistas que este orquestrador pode consultar automaticamente.
          </p>
        </CardHeader>

        <CardContent className="space-y-4">
          {loading ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="w-6 h-6 animate-spin text-primary" />
            </div>
          ) : delegations.length === 0 && !showForm ? (
            <div className="text-center py-8 text-muted-foreground">
              <Bot className="w-10 h-10 mx-auto mb-3 opacity-30" />
              <p className="text-sm">Nenhum especialista vinculado</p>
              <p className="text-xs mt-1">
                Vincule agentes para que este orquestrador delegue tarefas específicas.
              </p>
            </div>
          ) : (
            <div className="space-y-3">
              {delegations.map((d) => (
                <DelegationCard
                  key={d.id}
                  delegation={d}
                  backendUrl={BACKEND_URL}
                  onUpdate={loadDelegations}
                  onDelete={() => setDeleteDelegationId(d.id)}
                  toast={toast}
                />
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Add Form */}
      {showForm && (
        <Card className="bg-card border-primary/20 animate-in fade-in slide-in-from-top-2">
          <CardHeader className="pb-3">
            <CardTitle className="text-sm text-foreground flex items-center gap-2">
              <Settings2 className="w-4 h-4 text-primary " />
              Vincular Novo Especialista
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div>
              <Label className="text-muted-foreground text-xs">Agente Especialista *</Label>
              <Select value={selectedSubagentId} onValueChange={setSelectedSubagentId}>
                <SelectTrigger className="bg-background border-border text-foreground mt-1">
                  <SelectValue placeholder="Selecione um agente" />
                </SelectTrigger>
                <SelectContent className="bg-card border-border">
                  {filteredAgents.length === 0 ? (
                    <div className="p-3 text-xs text-muted-foreground text-center">
                      <AlertTriangle className="w-4 h-4 mx-auto mb-1 text-warning" />
                      Nenhum agente disponível para vincular
                    </div>
                  ) : (
                    filteredAgents.map((a) => (
                      <SelectItem key={a.id} value={a.id} className="text-foreground">
                        <div className="flex items-center gap-2">
                          <Bot className="w-3 h-3 text-primary " />
                          {a.name}
                          {a.is_subagent && (
                            <Badge className="text-[9px] px-1 py-0 bg-brand-muted text-primary border-primary/20">
                              Especialista
                            </Badge>
                          )}
                        </div>
                      </SelectItem>
                    ))
                  )}
                </SelectContent>
              </Select>
            </div>

            <div>
              <Label className="text-muted-foreground text-xs">Descrição da Tarefa *</Label>
              <Textarea
                value={taskDescription}
                onChange={(e) => setTaskDescription(e.target.value)}
                placeholder="Ex: Especialista em logística e rastreamento de pedidos. Delegar quando o cliente perguntar sobre entrega, frete ou status de pedido."
                className="bg-background border-border text-foreground text-xs h-20 mt-1"
              />
              <p className="text-[10px] text-muted-foreground mt-1">
                Descreva quando o orquestrador deve delegar para este especialista.
              </p>
            </div>

            <div className="grid grid-cols-3 gap-3">
              <div>
                <Label className="text-muted-foreground text-[10px]">Timeout (s)</Label>
                <Input
                  type="number"
                  min={5}
                  max={120}
                  value={timeoutSeconds}
                  onChange={(e) => setTimeoutSeconds(Number(e.target.value))}
                  className="bg-background border-border text-foreground text-xs mt-1"
                />
              </div>
              <div>
                <Label className="text-muted-foreground text-[10px]">Max Iterações</Label>
                <Input
                  type="number"
                  min={1}
                  max={15}
                  value={maxIterations}
                  onChange={(e) => setMaxIterations(Number(e.target.value))}
                  className="bg-background border-border text-foreground text-xs mt-1"
                />
              </div>
              <div>
                <Label className="text-muted-foreground text-[10px]">Contexto (chars)</Label>
                <Input
                  type="number"
                  min={500}
                  max={10000}
                  step={500}
                  value={maxContextChars}
                  onChange={(e) => setMaxContextChars(Number(e.target.value))}
                  className="bg-background border-border text-foreground text-xs mt-1"
                />
              </div>
            </div>

            <div className="flex gap-2 pt-2">
              <Button
                onClick={handleCreate}
                disabled={saving || !selectedSubagentId || !taskDescription.trim()}
                className="bg-primary hover:bg-primary/90 text-primary-foreground text-xs"
              >
                {saving ? (
                  <Loader2 className="w-3 h-3 mr-1 animate-spin" />
                ) : (
                  <Plus className="w-3 h-3 mr-1" />
                )}
                Vincular
              </Button>
              <Button
                variant="outline"
                onClick={() => {
                  setShowForm(false);
                  resetForm();
                }}
                className="text-muted-foreground border-border text-xs"
              >
                Cancelar
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      <ConfirmDialog
        open={!!deleteDelegationId}
        onOpenChange={(isOpen) => {
          if (!isOpen) setDeleteDelegationId(null);
        }}
        title="Remover especialista?"
        description="Esta ação remove o vínculo deste especialista com o orquestrador."
        confirmLabel="Remover"
        destructive
        onConfirm={() => {
          const delegationId = deleteDelegationId;
          setDeleteDelegationId(null);
          if (delegationId) {
            void handleDelete(delegationId);
          }
        }}
      />
    </div>
  );
}
