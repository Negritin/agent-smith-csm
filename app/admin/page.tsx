'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  Building2,
  Users,
  UserCheck,
  DollarSign,
  Activity,
  AlertCircle,
  FileText,
} from 'lucide-react';
import Link from 'next/link';
import { useAdminRole } from '@/hooks/useAdminRole';
import { LoadingState } from '@/components/ui/feedback-state';
import { MetricCard } from '@/components/ui/metric-card';
import {
  ObjectList,
  ObjectListActions,
  ObjectListItem,
  ObjectListMeta,
  ObjectListTitle,
} from '@/components/ui/object-list';
import {
  PageDescription,
  PageHeader,
  PageSection,
  PageShell,
  PageTitle,
} from '@/components/ui/page-shell';
import { StatusPill } from '@/components/ui/status-pill';

interface DashboardStats {
  totalCompanies: number;
  activeCompanies: number;
  suspendedCompanies: number;
  totalUsers: number;
  pendingUsers: number;
  activeUsers: number;
  suspendedUsers: number;
  mrr: number;
  logsLast24h: number;
  failedLoginsLast24h: number;
  errorsLast24h: number;
}

export default function AdminDashboardPage() {
  const router = useRouter();
  const { role, isLoading: roleLoading } = useAdminRole();
  const [stats, setStats] = useState<DashboardStats>({
    totalCompanies: 0,
    activeCompanies: 0,
    suspendedCompanies: 0,
    totalUsers: 0,
    pendingUsers: 0,
    activeUsers: 0,
    suspendedUsers: 0,
    mrr: 0,
    logsLast24h: 0,
    failedLoginsLast24h: 0,
    errorsLast24h: 0,
  });
  const [loading, setLoading] = useState(true);

  // Redirect Company Admin to their team page
  useEffect(() => {
    if (!roleLoading && role === 'company_admin') {
      router.push('/admin/team');
    }
  }, [role, roleLoading, router]);

  useEffect(() => {
    loadStats();
  }, []);

  const loadStats = async () => {
    try {
      const response = await fetch('/api/admin/stats');

      if (!response.ok) {
        throw new Error('Erro ao carregar estatísticas');
      }

      const data = await response.json();
      setStats(data);
    } catch (error) {
      console.error('Error loading stats:', error);
    } finally {
      setLoading(false);
    }
  };

  const statCards = [
    {
      title: 'MRR (Monthly Recurring Revenue)',
      value: `R$ ${stats.mrr.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`,
      icon: DollarSign,
      details: 'Receita mensal recorrente',
    },
    {
      title: 'Total de Empresas',
      value: stats.totalCompanies,
      icon: Building2,
      details: `${stats.activeCompanies} ativas, ${stats.suspendedCompanies} suspensas`,
    },
    {
      title: 'Total de Usuários',
      value: stats.totalUsers,
      icon: Users,
      details: `${stats.activeUsers} ativos, ${stats.suspendedUsers} suspensos`,
    },
    {
      title: 'Aprovações Pendentes',
      value: stats.pendingUsers,
      icon: UserCheck,
      details: 'Aguardando aprovação',
    },
  ];

  return (
    <PageShell>
      <PageHeader>
        <div>
          <PageTitle>Dashboard Administrativo</PageTitle>
          <PageDescription>Visão geral do sistema Smith</PageDescription>
        </div>
      </PageHeader>

      {loading ? (
        <LoadingState label="Carregando estatísticas..." />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
          {statCards.map((card) => (
            <MetricCard
              key={card.title}
              label={card.title}
              value={card.value}
              description={card.details}
              icon={card.icon}
              tone={
                card.title.includes('MRR')
                  ? 'success'
                  : card.title.includes('Pendentes')
                    ? 'warning'
                    : 'brand'
              }
            />
          ))}
        </div>
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <PageSection>
          <h2 className="text-lg font-semibold text-foreground">Ações Rápidas</h2>
          <ObjectList>
            <ObjectListItem>
              <Link
                href="/admin/pending-users"
                className="flex flex-1 items-center justify-between gap-4"
              >
                <div>
                  <ObjectListTitle>Aprovar Usuários</ObjectListTitle>
                  <ObjectListMeta>{stats.pendingUsers} pendentes</ObjectListMeta>
                </div>
                <ObjectListActions>
                  <UserCheck className="w-5 h-5 text-primary" />
                </ObjectListActions>
              </Link>
            </ObjectListItem>
            <ObjectListItem>
              <Link
                href="/admin/companies"
                className="flex flex-1 items-center justify-between gap-4"
              >
                <div>
                  <ObjectListTitle>Gerenciar Empresas</ObjectListTitle>
                  <ObjectListMeta>{stats.totalCompanies} cadastradas</ObjectListMeta>
                </div>
                <ObjectListActions>
                  <Building2 className="w-5 h-5 text-primary" />
                </ObjectListActions>
              </Link>
            </ObjectListItem>
            <ObjectListItem>
              <Link href="/admin/logs" className="flex flex-1 items-center justify-between gap-4">
                <div>
                  <ObjectListTitle>Ver Logs do Sistema</ObjectListTitle>
                  <ObjectListMeta>{stats.logsLast24h} eventos nas últimas 24h</ObjectListMeta>
                </div>
                <ObjectListActions>
                  <FileText className="w-5 h-5 text-primary" />
                </ObjectListActions>
              </Link>
            </ObjectListItem>
          </ObjectList>
        </PageSection>

        <PageSection>
          <h2 className="text-lg font-semibold text-foreground">Resumo do Sistema</h2>
          <ObjectList>
            <ObjectListItem>
              <ObjectListMeta>Usuários Ativos</ObjectListMeta>
              <StatusPill tone="success">{stats.activeUsers}</StatusPill>
            </ObjectListItem>
            <ObjectListItem>
              <ObjectListMeta>Usuários Pendentes</ObjectListMeta>
              <StatusPill tone="warning">{stats.pendingUsers}</StatusPill>
            </ObjectListItem>
            <ObjectListItem>
              <ObjectListMeta>Empresas Ativas</ObjectListMeta>
              <StatusPill tone="success">{stats.activeCompanies}</StatusPill>
            </ObjectListItem>
            <ObjectListItem>
              <ObjectListMeta>Empresas Suspensas</ObjectListMeta>
              <StatusPill tone="danger">{stats.suspendedCompanies}</StatusPill>
            </ObjectListItem>
          </ObjectList>
        </PageSection>
      </div>

      <PageSection>
        <div className="flex items-center justify-between gap-3">
          <h2 className="flex items-center gap-2 text-lg font-semibold text-foreground">
            <Activity className="h-5 w-5 text-primary" />
            Atividade do Sistema
          </h2>
          <Link
            href="/admin/logs"
            className="text-sm font-medium text-primary hover:text-primary/80"
          >
            Ver todos os logs
          </Link>
        </div>
        <div className="grid grid-cols-1 gap-6 md:grid-cols-3">
          <MetricCard
            label="Total de Logs"
            value={stats.logsLast24h}
            description="Eventos registrados nas últimas 24h"
            icon={FileText}
            tone="info"
          />
          <MetricCard
            label="Logins com Falha"
            value={stats.failedLoginsLast24h}
            description="Tentativas sem sucesso"
            icon={AlertCircle}
            tone="warning"
          />
          <MetricCard
            label="Erros do Sistema"
            value={stats.errorsLast24h}
            description="Requer atenção"
            icon={AlertCircle}
            tone="danger"
          />
        </div>
      </PageSection>
    </PageShell>
  );
}
