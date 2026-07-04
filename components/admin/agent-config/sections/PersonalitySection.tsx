'use client';

import { RefObject, useState } from 'react';
import { AvatarUpload } from '@/components/AvatarUpload';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from '@/components/ui/command';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import { Brain, Eye, FileCode, Globe, Headset, Plus } from 'lucide-react';

export const DEFAULT_SYSTEM_PROMPT = `Você é o Agent Smith, um assistente inteligente e prestativo.
Seja profissional, claro e objetivo nas suas respostas.
Se não souber a resposta, diga que não sabe.`;

export interface ContextVariable {
  tag: string;
  label: string;
  description: string;
  icon: string;
}

interface Props {
  agentId?: string;
  isCreateMode: boolean;
  isSubagent: boolean;
  name: string;
  avatarUrl: string;
  setAvatarUrl: (value: string) => void;
  contextVars: ContextVariable[];
  insertVariable: (tag: string) => void;
  promptRef: RefObject<HTMLTextAreaElement>;
  systemPrompt: string;
  setSystemPrompt: (value: string) => void;
  allowWebSearch: boolean;
  setAllowWebSearch: (value: boolean) => void;
  allowHumanHandoff: boolean;
  setAllowHumanHandoff: (value: boolean) => void;
  allowCsvAnalytics: boolean;
  setAllowCsvAnalytics: (value: boolean) => void;
  allowVision: boolean;
  setAllowVision: (value: boolean) => void;
  visionModel: string | undefined;
  setVisionModel: (value: string) => void;
}

export function PersonalitySection({
  agentId,
  isCreateMode,
  isSubagent,
  name,
  avatarUrl,
  setAvatarUrl,
  contextVars,
  insertVariable,
  promptRef,
  systemPrompt,
  setSystemPrompt,
  allowWebSearch,
  setAllowWebSearch,
  allowHumanHandoff,
  setAllowHumanHandoff,
  allowCsvAnalytics,
  setAllowCsvAnalytics,
  allowVision,
  setAllowVision,
  visionModel,
  setVisionModel,
}: Props) {
  const [varOpen, setVarOpen] = useState(false);
  return (
    <div className="space-y-4">
      {/* Avatar Upload Card */}
      <Card className="bg-card border-border">
        <CardHeader>
          <CardTitle className="text-sm text-foreground">Avatar do Agente</CardTitle>
          <p className="text-xs text-muted-foreground mt-1">
            Imagem que representa o agente nas conversas
          </p>
        </CardHeader>
        <CardContent>
          <div className="flex items-center gap-6">
            {agentId ? (
              <AvatarUpload
                currentImageUrl={avatarUrl}
                onUpload={(url) => setAvatarUrl(url)}
                uploadPath="agents"
                entityId={agentId}
                size="h-16 w-16"
                fallback={name}
              />
            ) : (
              <div className="h-16 w-16 rounded-full bg-background border-2 border-border flex items-center justify-center">
                <Brain className="h-6 w-6 text-muted-foreground" />
              </div>
            )}
            <div>
              <h3 className="text-sm font-medium text-foreground">
                {agentId ? 'Foto do Agente' : 'Salve primeiro para adicionar avatar'}
              </h3>
              <p className="text-xs text-muted-foreground">
                {agentId
                  ? 'Clique na imagem para alterar'
                  : 'Crie o agente e depois edite para adicionar'}
              </p>
            </div>
          </div>
        </CardContent>
      </Card>

      <Card className="bg-card border-border">
        <CardHeader>
          <CardTitle className="text-sm text-foreground">System Prompt</CardTitle>
          <p className="text-xs text-muted-foreground mt-1">
            Deixe em branco para usar o prompt padrão
          </p>
        </CardHeader>
        <CardContent className="space-y-2">
          {/* Insert Variable — busca (lupa) + lista rolável */}
          {!isCreateMode && contextVars.length > 0 && (
            <div className="flex justify-end">
              <Popover open={varOpen} onOpenChange={setVarOpen}>
                <PopoverTrigger asChild>
                  <Button variant="outline" size="sm" className="gap-1 text-xs">
                    <Plus className="h-3 w-3" /> Inserir Variável
                  </Button>
                </PopoverTrigger>
                <PopoverContent align="end" className="w-[340px] p-0 bg-card border-border">
                  <Command
                    filter={(value, search) =>
                      value.toLowerCase().includes(search.toLowerCase()) ? 1 : 0
                    }
                  >
                    <CommandInput
                      placeholder="Buscar variável..."
                      className="text-foreground"
                    />
                    <CommandList>
                      <CommandEmpty>Nenhuma variável encontrada.</CommandEmpty>
                      <CommandGroup>
                        {contextVars.map((v) => (
                          <CommandItem
                            key={v.tag}
                            value={`${v.label} ${v.tag}`}
                            onSelect={() => {
                              insertVariable(v.tag);
                              setVarOpen(false);
                            }}
                            className="cursor-pointer"
                          >
                            <div className="flex min-w-0 flex-col">
                              <span className="truncate text-foreground">{v.label}</span>
                              <code className="truncate text-[10px] text-accent">{v.tag}</code>
                            </div>
                          </CommandItem>
                        ))}
                      </CommandGroup>
                    </CommandList>
                  </Command>
                </PopoverContent>
              </Popover>
            </div>
          )}
          <Textarea
            ref={promptRef}
            value={systemPrompt}
            onChange={(e) => setSystemPrompt(e.target.value)}
            placeholder={`Deixe em branco para usar o padrão:\n\n${DEFAULT_SYSTEM_PROMPT}`}
            rows={20}
            className="bg-background border-border text-foreground font-mono text-sm resize-y min-h-[440px] overflow-y-auto leading-relaxed"
          />
        </CardContent>
      </Card>

      <Card className="bg-card border-border">
        <CardHeader>
          <CardTitle className="text-sm text-foreground">Ferramentas Disponíveis</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Web Search */}
          <div className="flex items-center justify-between">
            <div>
              <Label className="text-muted-foreground flex items-center gap-2">
                <Globe className="h-4 w-4 text-primary" />
                Busca na Web (Tavily AI)
              </Label>
              <p className="text-xs text-muted-foreground">
                Permite que o agente busque informações atuais na internet
              </p>
            </div>
            <Switch checked={allowWebSearch} onCheckedChange={setAllowWebSearch} />
          </div>

          {/* Human Handoff — hidden for SubAgents (they can't escalate) */}
          {!isSubagent && (
            <div className="border-t border-border pt-4 mt-4">
              <div className="flex items-center justify-between">
                <div>
                  <Label className="text-muted-foreground flex items-center gap-2">
                    <Headset className="h-4 w-4 text-accent" />
                    Solicitar Humano
                  </Label>
                  <p className="text-xs text-muted-foreground">
                    Permite que o agente transfira a conversa para um humano quando necessário
                  </p>
                </div>
                <Switch checked={allowHumanHandoff} onCheckedChange={setAllowHumanHandoff} />
              </div>
            </div>
          )}

          {/* CSV Analytics */}
          <div className="border-t border-border pt-4 mt-4">
            <div className="flex items-center justify-between">
              <div>
                <Label className="text-muted-foreground flex items-center gap-2">
                  <FileCode className="h-4 w-4 text-primary" />
                  Análise de Dados CSV
                </Label>
                <p className="text-xs text-muted-foreground">
                  Permite ordenar, filtrar e analisar dados de planilhas/tabelas
                </p>
              </div>
              <Switch checked={allowCsvAnalytics} onCheckedChange={setAllowCsvAnalytics} />
            </div>
          </div>

          {/* Vision */}
          <div className="border-t border-border pt-4 mt-4">
            <div className="flex items-center justify-between">
              <div>
                <Label className="text-muted-foreground flex items-center gap-2">
                  <Eye className="h-4 w-4 text-primary" />
                  Visão Computacional
                </Label>
                <p className="text-xs text-muted-foreground">
                  Permite analisar imagens enviadas (GPT-4o, Claude 3.5 Sonnet)
                </p>
              </div>
              <Switch checked={allowVision} onCheckedChange={setAllowVision} />
            </div>

            {allowVision && (
              <div className="mt-4 pt-4 border-t border-border/50 space-y-4">
                <div>
                  <Label htmlFor="vision_model" className="text-muted-foreground text-sm">
                    Modelo de Visão
                  </Label>
                  <Select value={visionModel || ''} onValueChange={setVisionModel}>
                    <SelectTrigger className="bg-background border-border text-foreground mt-2">
                      <SelectValue placeholder="Selecione o modelo de visão" />
                    </SelectTrigger>
                    <SelectContent className="bg-card border-border">
                      <SelectItem value="gpt-4o" className="text-foreground hover:bg-muted">
                        GPT-4o (OpenAI)
                      </SelectItem>
                      <SelectItem
                        value="claude-3-5-sonnet-20240620"
                        className="text-foreground hover:bg-muted"
                      >
                        Claude 3.5 Sonnet (Anthropic)
                      </SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
