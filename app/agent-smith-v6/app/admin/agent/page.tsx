'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Bot, Plus } from 'lucide-react';
import { useAdminRole } from '@/hooks/useAdminRole';
import { AgentFlowView } from '@/components/agents/AgentFlowView';
import type { AgentWithDelegations } from '@/components/agents/hooks/useAgentFlowLayout';
import { useToast } from '@/hooks/use-toast';
import { EmptyStatePanel, InlineNotice, LoadingState } from '@/components/ui/feedback-state';
import {
  PageActions,
  PageDescription,
  PageHeader,
  PageShell,
  PageTitle,
} from '@/components/ui/page-shell';

export default function AgentConfigPage() {
  const { role, companyId, isLoading } = useAdminRole();
  const router = useRouter();
  const { toast } = useToast();
  const [agents, setAgents] = useState<AgentWithDelegations[]>([]);
  const [loadingAgents, setLoadingAgents] = useState(false);
  const [archiveAgentId, setArchiveAgentId] = useState<string | null>(null);

  useEffect(() => {
    if (!isLoading && role !== 'company_admin') {
      router.push('/admin');
    }
  }, [role, isLoading, router]);

  useEffect(() => {
    if (companyId) {
      loadAgents();
    }
  }, [companyId]);

  const loadAgents = async () => {
    setLoadingAgents(true);
    try {
      const response = await fetch(`/api/admin/proxy/agents/company/${companyId}/with-delegations`);
      if (response.ok) {
        const data = await response.json();
        setAgents(data);
      } else {
        throw new Error('Failed to load agents');
      }
    } catch (error) {
      console.error('Error loading agents:', error);
      toast({
        title: 'Erro',
        description: 'Falha ao carregar agentes',
        variant: 'destructive',
      });
    } finally {
      setLoadingAgents(false);
    }
  };

  const handleCreateAgent = () => {
    router.push('/admin/agent/new');
  };

  const handleEditAgent = (agentId: string) => {
    router.push(`/admin/agent/${agentId}`);
  };

  const handleArchiveAgent = async (agentId: string) => {
    try {
      const response = await fetch(`/api/admin/proxy/agents/${agentId}`, {
        method: 'DELETE',
      });

      if (response.ok) {
        toast({
          title: 'Sucesso',
          description: 'Agente arquivado com sucesso',
        });
        loadAgents();
      } else {
        throw new Error('Failed to archive agent');
      }
    } catch (error) {
      console.error('Error archiving agent:', error);
      toast({
        title: 'Erro',
        description: 'Falha ao arquivar agente',
        variant: 'destructive',
      });
    }
  };

  if (isLoading) {
    return <LoadingState />;
  }

  if (!companyId) {
    return (
      <InlineNotice tone="danger" className="m-8">
        Erro: Empresa não encontrada
      </InlineNotice>
    );
  }

  return (
    <PageShell>
      <PageHeader>
        <div>
          <PageTitle className="flex items-center gap-3">
            <Bot className="w-8 h-8" />
            Gerenciar Agentes IA
          </PageTitle>
          <PageDescription>
            Crie e configure múltiplos agentes com diferentes personalidades e funções
          </PageDescription>
        </div>
        <PageActions>
          <Button onClick={handleCreateAgent} className="gap-2">
            <Plus className="w-4 h-4" />
            Criar Novo Agente
          </Button>
        </PageActions>
      </PageHeader>

      {/* Agents Grid */}
      {loadingAgents ? (
        <LoadingState label="Carregando agentes..." />
      ) : agents.length === 0 ? (
        <EmptyStatePanel
          icon={Bot}
          title="Nenhum agente criado ainda"
          description="Crie seu primeiro agente para começar a personalizar seu assistente."
          action={
            <Button onClick={handleCreateAgent} className="gap-2">
              <Plus className="w-4 h-4" />
              Criar Primeiro Agente
            </Button>
          }
        />
      ) : (
        <AgentFlowView agents={agents} onEdit={handleEditAgent} onArchive={setArchiveAgentId} />
      )}

      {/* Info Card */}
      <InlineNotice tone="brand" className="mt-6 font-normal">
        <p className="text-sm text-primary">
          <strong>Dica:</strong> Você pode criar agentes especializados para diferentes funções
          (vendas, suporte, atendimento) e vinculá-los a canais específicos como WhatsApp.
        </p>
      </InlineNotice>

      <ConfirmDialog
        open={!!archiveAgentId}
        onOpenChange={(isOpen) => {
          if (!isOpen) setArchiveAgentId(null);
        }}
        title="Arquivar agente?"
        description="Esta ação arquiva o agente e remove sua disponibilidade nas telas administrativas."
        confirmLabel="Arquivar"
        destructive
        onConfirm={() => {
          const agentId = archiveAgentId;
          setArchiveAgentId(null);
          if (agentId) {
            void handleArchiveAgent(agentId);
          }
        }}
      />
    </PageShell>
  );
}
