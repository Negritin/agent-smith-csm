'use client';

import { HttpTool, HttpToolForm } from '@/components/admin/HttpToolForm';
import { McpServersPanel } from '@/components/admin/agent-config/McpServerCard';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Brain, Edit2, Loader2, Plug, Plus, Terminal, Trash } from 'lucide-react';

interface Props {
  agentId?: string;
  companyId: string;
  isHydeEnabled: boolean;
  setIsHydeEnabled: (value: boolean) => void;
  toolsView: 'list' | 'form';
  setToolsView: (value: 'list' | 'form') => void;
  httpTools: HttpTool[];
  editingTool: HttpTool | null;
  setEditingTool: (tool: HttpTool | null) => void;
  loadingTools: boolean;
  setDeleteToolId: (toolId: string | null) => void;
  onSaveTool: (tool: HttpTool) => Promise<void>;
  onToolsChanged?: () => void;
}

export function ToolsSection({
  agentId,
  companyId,
  isHydeEnabled,
  setIsHydeEnabled,
  toolsView,
  setToolsView,
  httpTools,
  editingTool,
  setEditingTool,
  loadingTools,
  setDeleteToolId,
  onSaveTool,
  onToolsChanged,
}: Props) {
  return (
    <div className="space-y-6">
      {/* RAG Configuration */}
      <Card className="bg-card border-border">
        <CardHeader>
          <CardTitle className="text-sm text-foreground flex items-center gap-2">
            <Brain className="h-4 w-4 text-primary" />
            Configuração RAG (Base de Conhecimento)
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex items-center justify-between">
            <div className="space-y-0.5">
              <Label className="text-foreground">Busca Profunda (HyDE)</Label>
              <p className="text-xs text-muted-foreground">
                Ativa geração de resposta hipotética para melhorar buscas complexas.
                <br />
                <span className="text-warning">Aumenta o tempo de resposta em ~5-8s.</span>
              </p>
            </div>
            <Switch checked={isHydeEnabled} onCheckedChange={setIsHydeEnabled} />
          </div>
        </CardContent>
      </Card>

      {!agentId ? (
        <Card className="bg-brand-muted border-primary/30">
          <CardContent className="pt-4">
            <p className="text-primary text-sm">
              Salve o agente primeiro antes de configurar ferramentas HTTP.
            </p>
          </CardContent>
        </Card>
      ) : toolsView === 'list' ? (
        <div className="space-y-4">
          <div className="flex justify-between items-center">
            <div>
              <h3 className="text-lg font-semibold text-foreground flex items-center gap-2">
                <Plug className="w-5 h-5 text-primary" />
                Ferramentas HTTP
              </h3>
              <p className="text-sm text-muted-foreground">
                Configure integrações de API para o agente
              </p>
            </div>
            <Button
              onClick={() => {
                setEditingTool(null);
                setToolsView('form');
              }}
              className="bg-primary hover:bg-primary/90 text-primary-foreground"
            >
              <Plus className="w-4 h-4 mr-2" />
              Nova Ferramenta
            </Button>
          </div>

          {loadingTools ? (
            <div className="flex justify-center p-8">
              <Loader2 className="w-6 h-6 animate-spin text-primary" />
            </div>
          ) : httpTools.length === 0 ? (
            <Card className="bg-background border-border">
              <CardContent className="flex flex-col items-center justify-center py-12">
                <Terminal className="w-12 h-12 text-muted-foreground mb-4" />
                <p className="text-muted-foreground text-center">
                  Nenhuma ferramenta configurada.
                  <br />
                  <span className="text-sm text-muted-foreground">
                    Clique em "Nova Ferramenta" para adicionar.
                  </span>
                </p>
              </CardContent>
            </Card>
          ) : (
            <div className="space-y-3">
              {httpTools.map((tool) => (
                <Card
                  key={tool.id}
                  className="bg-background border-border hover:border-primary/30 transition-colors"
                >
                  <CardContent className="p-4 flex justify-between items-center">
                    <div>
                      <div className="flex items-center gap-2">
                        <Badge
                          variant="outline"
                          className="bg-primary text-primary-foreground border-transparent"
                        >
                          {tool.method}
                        </Badge>
                        <span className="font-mono text-foreground">{tool.name}</span>
                      </div>
                      <p className="text-sm text-muted-foreground mt-1 line-clamp-1">
                        {tool.description}
                      </p>
                    </div>
                    <div className="flex gap-2">
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8 text-muted-foreground hover:text-foreground"
                        onClick={() => {
                          setEditingTool(tool);
                          setToolsView('form');
                        }}
                      >
                        <Edit2 className="w-4 h-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8 text-muted-foreground hover:text-danger"
                        onClick={() => setDeleteToolId(tool.id!)}
                      >
                        <Trash className="w-4 h-4" />
                      </Button>
                    </div>
                  </CardContent>
                </Card>
              ))}
            </div>
          )}
        </div>
      ) : (
        <HttpToolForm
          initialData={editingTool}
          agentId={agentId}
          onSave={onSaveTool}
          onCancel={() => setToolsView('list')}
        />
      )}

      {/* Integrações MCP — curadoria de tools por servidor (data-driven) */}
      {agentId && toolsView === 'list' && (
        <div className="space-y-4">
          <div>
            <h3 className="text-lg font-semibold text-foreground flex items-center gap-2">
              <Plug className="w-5 h-5 text-primary" />
              Integrações MCP
            </h3>
            <p className="text-sm text-muted-foreground">
              Conecte servidores MCP e escolha exatamente quais tools este agente enxerga
            </p>
          </div>
          <McpServersPanel
            agentId={agentId}
            companyId={companyId}
            onToolsChanged={onToolsChanged}
          />
        </div>
      )}
    </div>
  );
}
