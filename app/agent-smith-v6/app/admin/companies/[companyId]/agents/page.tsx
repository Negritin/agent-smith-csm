'use client';

import { useEffect, useState } from 'react';
import { useRouter, useParams } from 'next/navigation';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Bot, Plus, ArrowLeft, Building2, Lock as LockIcon } from 'lucide-react';
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

export default function AdminCompanyAgentsPage() {
  const { role, isLoading: roleLoading } = useAdminRole();
  const router = useRouter();
  const params = useParams();
  const companyId = params.companyId as string;
  const { toast } = useToast();

  const [agents, setAgents] = useState<AgentWithDelegations[]>([]);
  const [loadingAgents, setLoadingAgents] = useState(false);
  const [companyName, setCompanyName] = useState<string>('');
  const [archiveAgentId, setArchiveAgentId] = useState<string | null>(null);

  // Verificar permissão Super Admin
  useEffect(() => {
    if (!roleLoading && role !== 'master') {
      router.push('/admin');
    }
  }, [role, roleLoading, router]);

  // Carregar nome da empresa
  useEffect(() => {
    if (companyId) {
      loadCompanyInfo();
      loadAgents();
    }
  }, [companyId]);

  const loadCompanyInfo = async () => {
    try {
      const response = await fetch(`/api/admin/company-info?companyId=${companyId}`, {
        credentials: 'include',
      });

      if (!response.ok) throw new Error('Failed to load company info');

      const data = await response.json();
      setCompanyName(data?.company_name || 'Empresa');
    } catch (error) {
      console.error('Error loading company info:', error);
    }
  };

  const loadAgents = async () => {
    setLoadingAgents(true);
    try {
      const response = await fetch(`/api/admin/agents/company/${companyId}/with-delegations`);
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
    router.push(`/admin/companies/${companyId}/agents/new`);
  };

  const handleEditAgent = (agentId: string) => {
    router.push(`/admin/companies/${companyId}/agents/${agentId}`);
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

  if (roleLoading) {
    return <LoadingState />;
  }

  if (role !== 'master') {
    return (
      <InlineNotice tone="danger" className="m-8">
        Acesso negado. Apenas Super Admin pode acessar esta página.
      </InlineNotice>
    );
  }

  return (
    <PageShell>
      <PageHeader>
        <div>
          <Button
            variant="ghost"
            onClick={() => router.push('/admin/companies')}
            className="mb-4 text-muted-foreground hover:text-foreground hover:bg-muted -ml-2"
          >
            <ArrowLeft className="w-4 h-4 mr-2" />
            Voltar para Empresas
          </Button>

          <div className="flex items-center gap-3 mb-2">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg border border-primary/15 bg-brand-muted text-primary">
              <Building2 className="w-5 h-5" />
            </div>
            <div>
              <p className="text-sm text-muted-foreground">Gerenciando agentes de</p>
              <PageTitle>{companyName}</PageTitle>
            </div>
          </div>
          <PageDescription>Configure os agentes desta empresa</PageDescription>
        </div>
        <PageActions>
          <Button onClick={handleCreateAgent} className="gap-2">
            <Plus className="w-4 h-4" />
            Novo Agente
          </Button>
        </PageActions>
      </PageHeader>

      {/* Agents Flow View */}
      {loadingAgents ? (
        <LoadingState label="Carregando agentes..." />
      ) : agents.length === 0 ? (
        <EmptyStatePanel
          icon={Bot}
          title="Nenhum agente criado ainda"
          description="Crie o primeiro agente para esta empresa."
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

      {/* Info */}
      <InlineNotice tone="brand" className="mt-6 font-normal">
        <p className="text-sm text-primary">
          <LockIcon className="w-4 h-4 inline-block mr-2 text-primary/80" />
          <strong>Modo Super Admin:</strong> Você está visualizando os agentes como administrador do
          sistema. As alterações feitas aqui afetarão diretamente a experiência do cliente.
        </p>
      </InlineNotice>

      <ConfirmDialog
        open={!!archiveAgentId}
        onOpenChange={(isOpen) => {
          if (!isOpen) setArchiveAgentId(null);
        }}
        title="Arquivar agente?"
        description="Esta ação arquiva o agente desta empresa."
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
