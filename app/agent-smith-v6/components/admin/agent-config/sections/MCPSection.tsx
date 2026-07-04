'use client';

import { McpServersPanel } from '@/components/admin/agent-config/McpServerCard';
import { Card, CardContent } from '@/components/ui/card';
import { Info } from 'lucide-react';

interface Props {
  agentId?: string;
  companyId: string;
  onToolsChanged?: () => void;
}

export function MCPSection({ agentId, companyId, onToolsChanged }: Props) {
  return (
    <div className="space-y-6">
      {!agentId ? (
        <Card className="bg-brand-muted border-primary/30">
          <CardContent className="pt-4">
            <p className="text-primary text-sm">
              Salve o agente primeiro antes de configurar integrações MCP.
            </p>
          </CardContent>
        </Card>
      ) : (
        <>
          <Card className="bg-primary border-transparent">
            <CardContent className="p-4">
              <div className="flex items-start gap-3">
                <Info className="h-5 w-5 text-primary-foreground mt-0.5" />
                <div>
                  <p className="text-sm text-primary-foreground">
                    <strong>MCP (Model Context Protocol)</strong> permite integrar este agente com
                    serviços externos.
                  </p>
                  <p className="text-xs text-primary-foreground/90 mt-1">
                    1. <strong>Conecte</strong> sua conta, 2. <strong>Habilite</strong> as tools, 3.{' '}
                    <strong>Use</strong> no prompt. A curadoria completa também está disponível na
                    seção Ferramentas.
                  </p>
                </div>
              </div>
            </CardContent>
          </Card>

          <McpServersPanel
            agentId={agentId}
            companyId={companyId}
            onToolsChanged={onToolsChanged}
          />
        </>
      )}
    </div>
  );
}
