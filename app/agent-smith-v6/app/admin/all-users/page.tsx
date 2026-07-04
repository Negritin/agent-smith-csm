'use client';

import { useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { User, Search, Filter, CheckCircle, XCircle } from 'lucide-react';
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
import { FilterActions, FilterBar, FilterGroup, SearchField } from '@/components/ui/filter-bar';
import { PageDescription, PageHeader, PageShell, PageTitle } from '@/components/ui/page-shell';
import { StatusPill } from '@/components/ui/status-pill';

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
  is_owner: boolean;
}

interface SafeCompany {
  id: string;
  company_name: string;
  status: string;
}

export default function AdminAllUsersPage() {
  const [users, setUsers] = useState<SafeUser[]>([]);
  const [companies, setCompanies] = useState<Record<string, SafeCompany>>({});
  const [loading, setLoading] = useState(true);
  const [searchTerm, setSearchTerm] = useState('');
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [processing, setProcessing] = useState<string | null>(null);

  const [currentPage, setCurrentPage] = useState(1);
  const itemsPerPage = 15;

  useEffect(() => {
    loadData();
  }, []);

  const loadData = async () => {
    try {
      const [usersResponse, companiesResponse] = await Promise.all([
        fetch('/api/admin/users'),
        fetch('/api/admin/companies'),
      ]);

      if (!usersResponse.ok || !companiesResponse.ok) {
        throw new Error('Erro ao carregar dados');
      }

      const usersData = await usersResponse.json();
      const companiesData = await companiesResponse.json();

      if (usersData.users) setUsers(usersData.users);
      if (companiesData.companies) {
        const companiesMap = companiesData.companies.reduce(
          (acc: Record<string, SafeCompany>, company: SafeCompany) => {
            acc[company.id] = company;
            return acc;
          },
          {} as Record<string, SafeCompany>,
        );
        setCompanies(companiesMap);
      }
    } catch (error) {
      console.error('Error loading data:', error);
    } finally {
      setLoading(false);
    }
  };

  const updateUserStatus = async (userId: string, newStatus: 'active' | 'suspended') => {
    setProcessing(userId);
    try {
      const user = users.find((u) => u.id === userId);
      const company = user?.company_id ? companies[user.company_id] : undefined;

      const response = await fetch('/api/admin/users/status', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ userId, status: newStatus }),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || 'Failed to update status');
      }

      await logSystemAction({
        userId,
        companyId: user?.company_id || undefined,
        actionType: newStatus === 'active' ? 'USER_ACTIVATED' : 'USER_SUSPENDED',
        resourceType: 'user',
        resourceId: userId,
        details: {
          userEmail: user?.email,
          userName: `${user?.first_name} ${user?.last_name}`,
          companyName: company?.company_name,
          newStatus,
        },
        status: 'success',
      });

      await loadData();
    } catch (error) {
      console.error('Error updating user:', error);

      await logSystemAction({
        actionType: 'USER_UPDATED',
        resourceType: 'user',
        resourceId: userId,
        details: { error: String(error), newStatus },
        status: 'error',
        errorMessage: 'Erro ao atualizar usuário',
      });

      alert('Erro ao atualizar usuário');
    } finally {
      setProcessing(null);
    }
  };

  const getStatusBadge = (status: string) => {
    switch (status) {
      case 'active':
        return <StatusPill tone="success">Ativo</StatusPill>;
      case 'pending':
        return <StatusPill tone="warning">Pendente</StatusPill>;
      case 'suspended':
        return <StatusPill tone="danger">Suspenso</StatusPill>;
      default:
        return <StatusPill>{status}</StatusPill>;
    }
  };

  const getRoleBadge = (user: SafeUser) => {
    if (
      user.is_owner &&
      (user.role === 'admin_company' || user.role === 'owner' || user.role === 'admin')
    ) {
      return <StatusPill tone="warning">Owner</StatusPill>;
    }

    if (user.role === 'admin_company' || user.role === 'owner' || user.role === 'admin') {
      return <StatusPill tone="brand">Admin</StatusPill>;
    }

    return <StatusPill tone="info">Membro</StatusPill>;
  };

  const filteredUsers = users.filter((user) => {
    const matchesSearch =
      user.first_name?.toLowerCase().includes(searchTerm.toLowerCase()) ||
      user.last_name?.toLowerCase().includes(searchTerm.toLowerCase()) ||
      user.email?.toLowerCase().includes(searchTerm.toLowerCase()) ||
      user.cpf?.includes(searchTerm);

    const matchesStatus = statusFilter === 'all' || user.status === statusFilter;

    return matchesSearch && matchesStatus;
  });

  const totalPages = Math.ceil(filteredUsers.length / itemsPerPage);
  const paginatedUsers = filteredUsers.slice(
    (currentPage - 1) * itemsPerPage,
    currentPage * itemsPerPage,
  );

  return (
    <PageShell>
      <PageHeader>
        <div>
          <PageTitle>Todos os Usuários</PageTitle>
          <PageDescription>Visualize e gerencie todos os usuários do sistema</PageDescription>
        </div>
      </PageHeader>

      <FilterBar>
        <FilterGroup>
          <SearchField
            icon={Search}
            placeholder="Buscar por nome, email ou CPF..."
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
          />
        </FilterGroup>
        <FilterActions>
          <Select value={statusFilter} onValueChange={setStatusFilter}>
            <SelectTrigger className="w-full sm:w-[200px]">
              <Filter className="w-4 h-4 mr-2" />
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Todos</SelectItem>
              <SelectItem value="active">Ativos</SelectItem>
              <SelectItem value="pending">Pendentes</SelectItem>
              <SelectItem value="suspended">Suspensos</SelectItem>
            </SelectContent>
          </Select>
        </FilterActions>
      </FilterBar>

      {loading ? (
        <LoadingState label="Carregando usuários..." />
      ) : (
        <div className="space-y-4">
          {paginatedUsers.map((user) => (
            <DataCard key={user.id}>
              <DataCardHeader>
                <DataCardIdentity>
                  <DataCardIcon icon={User} />
                  <div className="min-w-0">
                    <DataCardTitle>
                      {user.first_name} {user.last_name}
                    </DataCardTitle>
                    <DataCardMeta>{user.email}</DataCardMeta>
                  </div>
                </DataCardIdentity>
                <DataCardBadges>
                  {getRoleBadge(user)}
                  {getStatusBadge(user.status)}
                </DataCardBadges>
              </DataCardHeader>
              <DataCardBody>
                <DataFieldGrid>
                  <DataField label="CPF" value={user.cpf} />
                  <DataField
                    label="Empresa"
                    value={
                      user.company_id && companies[user.company_id]
                        ? companies[user.company_id].company_name
                        : 'Não atribuída'
                    }
                  />
                  <DataField label="Telefone" value={user.phone || 'Não informado'} />
                  <DataField
                    label="Cadastro"
                    value={new Date(user.created_at).toLocaleDateString('pt-BR')}
                  />
                </DataFieldGrid>

                {user.status !== 'pending' && (
                  <DataCardActions>
                    {user.status === 'suspended' ? (
                      <Button
                        onClick={() => updateUserStatus(user.id, 'active')}
                        disabled={processing === user.id}
                      >
                        <CheckCircle className="w-4 h-4 mr-2" />
                        {processing === user.id ? 'Ativando...' : 'Ativar Usuário'}
                      </Button>
                    ) : (
                      <Button
                        onClick={() => updateUserStatus(user.id, 'suspended')}
                        disabled={processing === user.id}
                        variant="destructive"
                      >
                        <XCircle className="w-4 h-4 mr-2" />
                        {processing === user.id ? 'Suspendendo...' : 'Suspender Usuário'}
                      </Button>
                    )}
                  </DataCardActions>
                )}
              </DataCardBody>
            </DataCard>
          ))}

          {filteredUsers.length === 0 && (
            <EmptyStatePanel
              icon={User}
              title="Nenhum usuário encontrado"
              description="Ajuste os filtros ou aguarde novos cadastros."
            />
          )}

          {totalPages > 1 && (
            <div className="flex items-center justify-between rounded-lg border border-border bg-card px-4 py-4">
              <p className="text-sm text-muted-foreground">
                Mostrando {(currentPage - 1) * itemsPerPage + 1} -{' '}
                {Math.min(currentPage * itemsPerPage, filteredUsers.length)} de{' '}
                {filteredUsers.length}
              </p>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setCurrentPage((p) => Math.max(1, p - 1))}
                  disabled={currentPage === 1}
                >
                  Anterior
                </Button>
                <span className="px-2 text-sm text-muted-foreground">
                  {currentPage} / {totalPages}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setCurrentPage((p) => Math.min(totalPages, p + 1))}
                  disabled={currentPage === totalPages}
                >
                  Próximo
                </Button>
              </div>
            </div>
          )}
        </div>
      )}
    </PageShell>
  );
}
