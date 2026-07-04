'use client';

import { useState, useEffect } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { Switch } from '@/components/ui/switch';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Loader2, Brain, Trash2 } from 'lucide-react';
import { useToast } from '@/hooks/use-toast';

interface Props {
  agentId: string;
}

interface MemorySettings {
  whatsapp_summarization_mode: string;
  whatsapp_sliding_window_size: number;
  whatsapp_message_threshold: number;
  web_summarization_mode: string;
  web_message_threshold: number;
  extract_user_profile: boolean;
  extract_session_summary: boolean;
}

export function MemoryConfigTab({ agentId }: Props) {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [settings, setSettings] = useState<MemorySettings>({
    whatsapp_summarization_mode: 'sliding_window',
    whatsapp_sliding_window_size: 20,
    whatsapp_message_threshold: 30,
    web_summarization_mode: 'session_end',
    web_message_threshold: 20,
    extract_user_profile: true,
    extract_session_summary: true,
  });

  // Debug state
  const [debugUserId, setDebugUserId] = useState('');
  const [debugData, setDebugData] = useState<any>(null);
  const [debugLoading, setDebugLoading] = useState(false);
  const [deleteMemoryOpen, setDeleteMemoryOpen] = useState(false);

  const { toast } = useToast();

  useEffect(() => {
    loadSettings();
  }, [agentId]);

  const loadSettings = async () => {
    setLoading(true);
    try {
      const response = await fetch(`/api/admin/memory/settings?agentId=${agentId}`);
      if (response.ok) {
        const data = await response.json();
        setSettings(data);
      }
    } catch (error) {
      console.error('Error loading memory settings:', error);
      toast({
        title: 'Erro',
        description: 'Falha ao carregar configurações de memória',
        variant: 'destructive',
      });
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const response = await fetch('/api/admin/memory/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          agentId,
          ...settings,
        }),
      });

      if (response.ok) {
        toast({
          title: 'Sucesso',
          description: 'Configurações de memória salvas',
        });
      } else {
        throw new Error('Failed to save');
      }
    } catch (error) {
      toast({
        title: 'Erro',
        description: 'Falha ao salvar configurações',
        variant: 'destructive',
      });
    } finally {
      setSaving(false);
    }
  };

  const handleDebugFetch = async () => {
    if (!debugUserId.trim()) {
      toast({
        title: 'Atenção',
        description: 'Digite um ID de usuário (UUID)',
        variant: 'destructive',
      });
      return;
    }

    setDebugLoading(true);
    try {
      const response = await fetch(`/api/admin/memory/user/${debugUserId}`);
      if (response.ok) {
        const data = await response.json();
        setDebugData(data);
        toast({
          title: 'Dados carregados',
          description: `${data.total_summaries} resumos encontrados`,
        });
      } else {
        throw new Error('User not found');
      }
    } catch (error) {
      toast({
        title: 'Erro',
        description: 'Usuário não encontrado ou sem memória',
        variant: 'destructive',
      });
      setDebugData(null);
    } finally {
      setDebugLoading(false);
    }
  };

  const handleDebugDelete = async () => {
    if (!debugUserId.trim()) return;

    setDebugLoading(true);
    try {
      const response = await fetch(`/api/admin/memory/user/${debugUserId}`, {
        method: 'DELETE',
      });

      if (response.ok) {
        toast({
          title: 'Memória apagada',
          description: 'Todos os dados do usuário foram removidos',
        });
        setDebugData(null);
        setDebugUserId('');
      } else {
        throw new Error('Failed to delete');
      }
    } catch (error) {
      toast({
        title: 'Erro',
        description: 'Falha ao apagar memória',
        variant: 'destructive',
      });
    } finally {
      setDebugLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center p-8">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* WhatsApp Settings */}
      <Card className="bg-card border-border">
        <CardHeader>
          <CardTitle className="text-sm text-foreground flex items-center gap-2">
            WhatsApp
          </CardTitle>
          <p className="text-xs text-muted-foreground mt-1">
            Configurações de memória para conversas do WhatsApp
          </p>
        </CardHeader>
        <CardContent className="space-y-4">
          <div>
            <Label className="text-muted-foreground">Modo de Sumarização</Label>
            <Select
              value={settings.whatsapp_summarization_mode}
              onValueChange={(value) =>
                setSettings({ ...settings, whatsapp_summarization_mode: value })
              }
            >
              <SelectTrigger className="bg-background border-border text-foreground">
                <SelectValue />
              </SelectTrigger>
              <SelectContent className="bg-card border-border">
                <SelectItem value="sliding_window" className="text-foreground">
                  Janela Deslizante (Recomendado)
                </SelectItem>
                <SelectItem value="message_count" className="text-foreground">
                  Contagem de Mensagens
                </SelectItem>
                <SelectItem value="time_based" className="text-foreground">
                  Baseado em Tempo
                </SelectItem>
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground mt-1">
              {settings.whatsapp_summarization_mode === 'sliding_window' &&
                'Mantém últimas N mensagens raw, sumariza as antigas'}
              {settings.whatsapp_summarization_mode === 'message_count' &&
                'Sumariza a cada X mensagens'}
              {settings.whatsapp_summarization_mode === 'time_based' &&
                'Sumariza após X horas de inatividade'}
            </p>
          </div>

          {settings.whatsapp_summarization_mode === 'sliding_window' && (
            <div>
              <Label className="text-muted-foreground">Tamanho da Janela (mensagens)</Label>
              <Input
                type="number"
                value={settings.whatsapp_sliding_window_size}
                onChange={(e) =>
                  setSettings({
                    ...settings,
                    whatsapp_sliding_window_size: parseInt(e.target.value),
                  })
                }
                min={10}
                max={100}
                className="bg-background border-border text-foreground"
              />
              <p className="text-xs text-muted-foreground mt-1">
                Quantas mensagens recentes manter sem sumarização
              </p>
            </div>
          )}

          <div>
            <Label className="text-muted-foreground">
              {settings.whatsapp_summarization_mode === 'sliding_window'
                ? 'Gatilho de Sumarização'
                : 'Threshold de Mensagens'}
            </Label>
            <Input
              type="number"
              value={settings.whatsapp_message_threshold}
              onChange={(e) =>
                setSettings({
                  ...settings,
                  whatsapp_message_threshold: parseInt(e.target.value),
                })
              }
              min={settings.whatsapp_sliding_window_size || 10}
              max={200}
              className="bg-background border-border text-foreground"
            />
            <p className="text-xs text-muted-foreground mt-1">
              {settings.whatsapp_summarization_mode === 'sliding_window'
                ? `Sumariza quando atingir ${settings.whatsapp_message_threshold} mensagens (${settings.whatsapp_message_threshold - settings.whatsapp_sliding_window_size} antigas)`
                : 'Número de mensagens antes de sumarizar'}
            </p>
          </div>
        </CardContent>
      </Card>

      {/* Web Settings */}
      <Card className="bg-card border-border">
        <CardHeader>
          <CardTitle className="text-sm text-foreground flex items-center gap-2">Web</CardTitle>
          <p className="text-xs text-muted-foreground mt-1">
            Configurações de memória para chat web
          </p>
        </CardHeader>
        <CardContent className="space-y-4">
          <div>
            <Label className="text-muted-foreground">Modo de Sumarização</Label>
            <Select
              value={settings.web_summarization_mode}
              onValueChange={(value) => setSettings({ ...settings, web_summarization_mode: value })}
            >
              <SelectTrigger className="bg-background border-border text-foreground">
                <SelectValue />
              </SelectTrigger>
              <SelectContent className="bg-card border-border">
                <SelectItem value="session_end" className="text-foreground">
                  Fim da Sessão (Padrão)
                </SelectItem>
                <SelectItem value="message_count" className="text-foreground">
                  Contagem de Mensagens
                </SelectItem>
                <SelectItem value="inactivity" className="text-foreground">
                  Inatividade
                </SelectItem>
              </SelectContent>
            </Select>
          </div>

          {/* Mostrar threshold se modo for message_count ou inactivity */}
          {(settings.web_summarization_mode === 'message_count' ||
            settings.web_summarization_mode === 'inactivity') && (
            <div>
              <Label className="text-muted-foreground">
                {settings.web_summarization_mode === 'message_count'
                  ? 'Threshold de Mensagens'
                  : 'Mensagens antes de considerar inativo'}
              </Label>
              <Input
                type="number"
                value={settings.web_message_threshold}
                onChange={(e) =>
                  setSettings({
                    ...settings,
                    web_message_threshold: parseInt(e.target.value),
                  })
                }
                min={10}
                max={200}
                className="bg-background border-border text-foreground"
              />
              <p className="text-xs text-muted-foreground mt-1">
                {settings.web_summarization_mode === 'message_count'
                  ? 'Número de mensagens acumuladas antes de sumarizar'
                  : 'Mensagens mínimas para considerar sessão ativa'}
              </p>
            </div>
          )}

          <div className="flex items-center justify-between">
            <div>
              <Label className="text-muted-foreground">Extrair Perfil do Usuário</Label>
              <p className="text-xs text-muted-foreground">
                Extrai fatos duráveis sobre o usuário (nome, preferências, etc)
              </p>
            </div>
            <Switch
              checked={settings.extract_user_profile}
              onCheckedChange={(checked) =>
                setSettings({ ...settings, extract_user_profile: checked })
              }
            />
          </div>

          <div className="flex items-center justify-between">
            <div>
              <Label className="text-muted-foreground">Gerar Resumo de Sessão</Label>
              <p className="text-xs text-muted-foreground">
                Cria resumos de conversas anteriores para contexto
              </p>
            </div>
            <Switch
              checked={settings.extract_session_summary}
              onCheckedChange={(checked) =>
                setSettings({ ...settings, extract_session_summary: checked })
              }
            />
          </div>
        </CardContent>
      </Card>

      {/* Debug Tools */}
      <Card className="bg-card border-border">
        <CardHeader>
          <CardTitle className="text-sm text-foreground flex items-center gap-2">
            <Brain className="h-4 w-4" />
            Debug: Visualizar Memória
          </CardTitle>
          <p className="text-xs text-muted-foreground mt-1">
            Inspecione ou apague a memória de um usuário específico
          </p>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex gap-2">
            <Input
              placeholder="ID do Usuário (UUID)"
              value={debugUserId}
              onChange={(e) => setDebugUserId(e.target.value)}
              className="bg-background border-border text-foreground flex-1"
            />
            <Button
              onClick={handleDebugFetch}
              disabled={debugLoading}
              variant="outline"
              className="gap-2"
            >
              {debugLoading ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Brain className="h-4 w-4" />
              )}
              Buscar
            </Button>
            {debugData && (
              <Button
                onClick={() => setDeleteMemoryOpen(true)}
                disabled={debugLoading}
                variant="destructive"
                className="gap-2"
              >
                <Trash2 className="h-4 w-4" />
                Apagar
              </Button>
            )}
          </div>

          {debugData && (
            <div className="bg-background border border-border rounded-lg p-4 max-h-[400px] overflow-auto">
              <pre className="text-xs text-muted-foreground whitespace-pre-wrap break-words">
                {JSON.stringify(debugData, null, 2)}
              </pre>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Save Button */}
      <Button
        onClick={handleSave}
        disabled={saving}
        className="w-full bg-primary hover:bg-primary/90 text-primary-foreground"
      >
        {saving ? (
          <>
            <Loader2 className="h-4 w-4 animate-spin mr-2" />
            Salvando...
          </>
        ) : (
          'Salvar Configurações de Memória'
        )}
      </Button>

      <ConfirmDialog
        open={deleteMemoryOpen}
        onOpenChange={setDeleteMemoryOpen}
        title="Apagar memória do usuário?"
        description="Esta ação remove os dados de memória deste usuário e não pode ser desfeita."
        confirmLabel="Apagar memória"
        destructive
        onConfirm={handleDebugDelete}
      />
    </div>
  );
}
