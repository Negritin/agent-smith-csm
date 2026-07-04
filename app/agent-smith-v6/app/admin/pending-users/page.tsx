'use client';

import { useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { UserCheck, CheckCircle, XCircle, User } from 'lucide-react';
import { logSystemAction } from '@/lib/logger';
import {
  DataCard,
  DataCardActions,
  DataCardBadges,
  DataCardBody,
  DataCardHeader,
  DataCardIcon,
  DataCardIdentity,
  DataCardMeta,
  DataCardTitle,
  DataField,
  DataFieldGrid,
} from '@/components/ui/data-card';
import { EmptyStatePanel, LoadingState } from '@/components/ui/feedback-state';
import { PageDescription, PageHeader, PageShell, PageTitle } from '@/components/ui/page-shell';
import { StatusPill } from '@/components/ui/status-pill';
import { useToast } from '@/hooks/use-toast';

interface SafeUser {
  id: string;
  email: string;
  first_name: string;
  last_name: string;
  role: string;
  status: string;
  company_id: string | null;
  created_at: string;
  phone: string | null;
  cpf: string;
}

interface SafeCompany {
  id: string;
  company_name: string;
  status: string;
}

export default function AdminPendingUsersPage() {
  const { toast } = useToast();
  const [users, setUsers] = useState<SafeUser[]>([]);
  const [companies, setCompanies] = useState<SafeCompany[]>([]);
  const [loading, setLoading] = useState(true);
  const [processing, setProcessing] = useState<string | null>(null);
  const [rejectUserId, setRejectUserId] = useState<string | null>(null);

  useEffect(() => {
    loadData();
  }, []);

  const loadData = async () => {
    try {
      const [usersResponse, companiesResponse] = await Promise.all([
        fetch('/api/admin/users'),
        fetch('/api/admin/companies?status=active'),
      ]);

      if (!usersResponse.ok || !companiesResponse.ok) {
        throw new Error('Erro ao carregar dados');
      }

      const usersData = await usersResponse.json();
      const companiesData = await companiesResponse.json();

      const pendingUsers = (usersData.users || []).filter((u: SafeUser) => u.status === 'pending');
      setUsers(pendingUsers);
      setCompanies(companiesData.companies || []);
    } catch (error) {
      console.error('Error loading data:', error);
    } finally {
      setLoading(false);
    }
  };

  const approveUser = async (userId: string, companyId: string) => {
    if (!companyId) {
      toast({
        title: 'Atenção',
        description: 'Selecione uma empresa para continuar.',
        variant: 'destructive',
      });
      return;
    }

    setProcessing(userId);
    try {
      const user = users.find((u) => u.id === userId);
      const company = companies.find((c) => c.id === companyId);

      const response = await fetch('/api/admin/users/status', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ userId, action: 'approve', companyId }),
      });

      if (!response.ok) {
        throw new Error('Failed to approve');
      }

      await logSystemAction({
        userId,
        companyId,
        actionType: 'USER_APPROVED',
        resourceType: 'user',
        resourceId: userId,
        details: {
          userEmail: user?.email,
          userName: `${user?.first_name} ${user?.last_name}`,
          companyName: company?.company_name,
        },
        status: 'success',
      });

      await loadData();
    } catch (error) {
      console.error('Error approving user:', error);

      await logSystemAction({
        actionType: 'USER_APPROVED',
        resourceType: 'user',
        resourceId: userId,
        details: { reason: 'approval_failed' },
        status: 'error',
        errorMessage: 'Erro ao aprovar usuário',
      });

      toast({
        title: 'Erro',
        description: 'Não foi possível aprovar o usuário.',
        variant: 'destructive',
      });
    } finally {
      setProcessing(null);
    }
  };

  const rejectUser = async (userId: string) => {
    setProcessing(userId);
    try {
      const user = users.find((u) => u.id === userId);

      const response = await fetch('/api/admin/users/status', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ userId, action: 'reject' }),
      });

      if (!response.ok) {
        throw new Error('Failed to reject');
      }

      await logSystemAction({
        userId,
        actionType: 'USER_REJECTED',
        resourceType: 'user',
        resourceId: userId,
        details: {
          userEmail: user?.email,
          userName: `${user?.first_name} ${user?.last_name}`,
        },
        status: 'success',
      });

      await loadData();
    } catch (error) {
      console.error('Error rejecting user:', error);

      await logSystemAction({
        actionType: 'USER_REJECTED',
        resourceType: 'user',
        resourceId: userId,
        details: { reason: 'rejection_failed' },
        status: 'error',
        errorMessage: 'Erro ao rejeitar usuário',
      });

      toast({
        title: 'Erro',
        description: 'Não foi possível rejeitar o usuário.',
        variant: 'destructive',
      });
    } finally {
      setProcessing(null);
    }
  };

  return (
    <PageShell>
      <PageHeader>
        <div>
          <PageTitle>Aprovações Pendentes</PageTitle>
          <PageDescription>Revise e aprove novos usuários no sistema</PageDescription>
        </div>
      </PageHeader>

      {loading ? (
        <LoadingState label="Carregando usuários pendentes..." />
      ) : (
        <div className="space-y-4">
          {users.map((user) => (
            <DataCard key={user.id}>
              <DataCardHeader>
                <DataCardIdentity>
                  <DataCardIcon icon={User} tone="warning" />
                  <div className="min-w-0">
                    <DataCardTitle>
                      {user.first_name} {user.last_name}
                    </DataCardTitle>
                    <DataCardMeta>{user.email}</DataCardMeta>
                  </div>
                </DataCardIdentity>
                <DataCardBadges>
                  <StatusPill tone="warning">Pendente</StatusPill>
                </DataCardBadges>
              </DataCardHeader>
              <DataCardBody>
                <DataFieldGrid className="lg:grid-cols-3">
                  <DataField label="CPF" value={user.cpf} />
                  <DataField label="Telefone" value={user.phone || 'Não informado'} />
                  <DataField
                    label="Data de Cadastro"
                    value={new Date(user.created_at).toLocaleDateString('pt-BR')}
                  />
                </DataFieldGrid>

                <DataCardActions className="flex-col items-stretch sm:flex-row">
                  <div className="flex-1">
                    <Select
                      onValueChange={(value) => {
                        const userElement = document.getElementById(`company-${user.id}`);
                        if (userElement) userElement.dataset.companyId = value;
                      }}
                    >
                      <SelectTrigger>
                        <SelectValue placeholder="Selecione uma empresa" />
                      </SelectTrigger>
                      <SelectContent>
                        {companies.map((company) => (
                          <SelectItem key={company.id} value={company.id}>
                            {company.company_name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="flex gap-2" id={`company-${user.id}`}>
                    <Button
                      onClick={() => {
                        const companyId = document.getElementById(`company-${user.id}`)?.dataset
                          .companyId;
                        approveUser(user.id, companyId || '');
                      }}
                      disabled={processing === user.id}
                    >
                      <CheckCircle className="w-4 h-4 mr-2" />
                      {processing === user.id ? 'Aprovando...' : 'Aprovar'}
                    </Button>
                    <Button
                      onClick={() => setRejectUserId(user.id)}
                      disabled={processing === user.id}
                      variant="destructive"
                    >
                      <XCircle className="w-4 h-4 mr-2" />
                      Rejeitar
                    </Button>
                  </div>
                </DataCardActions>
              </DataCardBody>
            </DataCard>
          ))}

          {users.length === 0 && (
            <EmptyStatePanel
              icon={UserCheck}
              title="Nenhuma aprovação pendente"
              description="Todos os usuários foram processados. Novos cadastros aparecerão aqui."
            />
          )}
        </div>
      )}

      <ConfirmDialog
        open={!!rejectUserId}
        onOpenChange={(isOpen) => {
          if (!isOpen) setRejectUserId(null);
        }}
        title="Rejeitar usuário?"
        description="Esta ação rejeita a solicitação de acesso do usuário."
        confirmLabel="Rejeitar"
        destructive
        onConfirm={() => {
          const userId = rejectUserId;
          setRejectUserId(null);
          if (userId) {
            void rejectUser(userId);
          }
        }}
      />
    </PageShell>
  );
}
