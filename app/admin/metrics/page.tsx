'use client';

import { useEffect, useState } from 'react';
import {
  BarChart3,
  Calendar,
  MessageSquare,
  Users,
  UserPlus,
  Repeat,
  Coins,
  Headphones,
  Bot,
  Timer,
  CheckCircle2,
  AlertTriangle,
} from 'lucide-react';
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from 'recharts';
import { MetricCard } from '@/components/ui/metric-card';
import { RankingBars } from '@/components/ui/ranking-bars';
import { BreachList, type SlaBreach } from '@/components/ui/breach-list';
import { Skeleton } from '@/components/ui/skeleton';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { PageDescription, PageHeader, PageShell, PageTitle } from '@/components/ui/page-shell';

// ========== TYPES ==========

interface MetricsSummary {
  total_conversations: number;
  total_messages: number;
  new_conversations: number;
  existing_conversations: number;
  leads_generated: number;
  // S4 rpc_metrics_summary devolve o consumo em BRL (round(sum(abs(amount_brl)),2)).
  credits_consumed_brl: number;
}

interface TimeseriesPoint {
  date: string;
  conversations: number;
  messages: number;
}

// Subconjunto de /api/billing/subscription — só o necessário p/ converter BRL→créditos
// (mesma lógica do billing: app/admin/billing/page.tsx:250-256).
interface MetricsSubscription {
  plan: {
    price_brl: number;
    display_credits: number;
  } | null;
}

const EMPTY_SUMMARY: MetricsSummary = {
  total_conversations: 0,
  total_messages: 0,
  new_conversations: 0,
  existing_conversations: 0,
  leads_generated: 0,
  credits_consumed_brl: 0,
};

// ----- Aba "Atendimentos" (SPEC §4) -----
interface AttendanceAdminRow {
  user_id: string | null;
  name: string | null;
  role: string | null;
  is_owner: boolean | null;
  taken: number;
  resolved: number;
  open: number;
}

interface AttendanceSla {
  first_response_pct: number | null;
  resolution_pct: number | null;
  breached_count: number;
  breaches: SlaBreach[];
}

interface AttendanceData {
  by_admin: AttendanceAdminRow[];
  sla: AttendanceSla;
}

const EMPTY_ATTENDANCE: AttendanceData = {
  by_admin: [],
  sla: { first_response_pct: null, resolution_pct: null, breached_count: 0, breaches: [] },
};

// ----- Aba "Agentes" (SPEC §5) -----
interface AgentRow {
  agent_id: string | null;
  agent_name: string | null;
  messages: number;
  conversations: number;
}

export default function MetricsPage() {
  const [activeTab, setActiveTab] = useState('geral');
  const [selectedPeriod, setSelectedPeriod] = useState('30');
  // Custom date range states (copy billing 147-151)
  const [customStartDate, setCustomStartDate] = useState('');
  const [customEndDate, setCustomEndDate] = useState('');

  const [summary, setSummary] = useState<MetricsSummary>(EMPTY_SUMMARY);
  const [series, setSeries] = useState<TimeseriesPoint[]>([]);
  const [subscription, setSubscription] = useState<MetricsSubscription | null>(null);
  const [loading, setLoading] = useState(true);
  const [accessError, setAccessError] = useState<string | null>(null);

  // ----- Aba "Atendimentos" (fetch lazy por aba ativa) -----
  const [attendance, setAttendance] = useState<AttendanceData>(EMPTY_ATTENDANCE);
  const [attendanceLoading, setAttendanceLoading] = useState(false);
  const [attendanceError, setAttendanceError] = useState<string | null>(null);

  // ----- Aba "Agentes" (fetch lazy por aba ativa) -----
  const [agents, setAgents] = useState<AgentRow[]>([]);
  const [agentsLoading, setAgentsLoading] = useState(false);
  const [agentsError, setAgentsError] = useState<string | null>(null);

  // ----- helpers (copy billing) -----
  const formatNumber = (value: number) => new Intl.NumberFormat('pt-BR').format(value);

  // Converte BRL→créditos com a MESMA lógica do billing (app/admin/billing/page.tsx:250-256):
  // round((brl / plan.price_brl) * plan.display_credits). 'Créditos' é contagem, não R$.
  const brlToCredits = (brlValue: number): number => {
    if (!subscription?.plan) return 0;
    const planPrice = subscription.plan.price_brl || 1;
    const displayCredits = subscription.plan.display_credits || 0;
    if (planPrice === 0) return 0;
    return Math.round((brlValue / planPrice) * displayCredits);
  };

  const getLocalDateString = (date: Date): string => {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
  };

  const formatShortDate = (dateString: string) =>
    new Date(dateString).toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit' });

  const formatDate = (dateString: string | null) => {
    if (!dateString) return '-';
    return new Date(dateString).toLocaleDateString('pt-BR', {
      day: '2-digit',
      month: '2-digit',
      year: 'numeric',
    });
  };

  // SLA pct vem do backend JÁ em escala 0–100 (a RPC faz round(... * 100, 1));
  // null = sem denominador → "—". NÃO re-escalar (multiplicar por 100 invertia 1% → 100%).
  const formatPct = (value: number | null): string => {
    if (value == null || Number.isNaN(value)) return '—';
    return `${Math.round(value)}%`;
  };

  // Nome de exibição do admin (fallback p/ sessões sem join de nome).
  const adminDisplayName = (row: AttendanceAdminRow): string =>
    row.name?.trim() || 'Sem identificação';

  const getPeriodLabel = () => {
    switch (selectedPeriod) {
      case 'today':
        return `Hoje (${new Date().toLocaleDateString('pt-BR')})`;
      case '7':
        return 'últimos 7 dias';
      case '30':
        return 'últimos 30 dias';
      case '90':
        return 'últimos 3 meses';
      case 'custom':
        if (customStartDate && customEndDate) {
          return `${customStartDate.split('-').reverse().join('/')} a ${customEndDate
            .split('-')
            .reverse()
            .join('/')}`;
        }
        return 'período personalizado';
      default:
        return `últimos ${selectedPeriod} dias`;
    }
  };

  // ----- plano p/ conversão BRL→créditos (mirror billing 154-179, fetch único no mount) -----
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch('/api/billing/subscription');
        if (res.ok && !cancelled) {
          setSubscription((await res.json()) as MetricsSubscription);
        }
      } catch (err) {
        console.error('Error fetching subscription:', err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Monta a querystring de período (mesma lógica usada pela Geral) — reaproveitada
  // pelos fetches lazy de Atendimentos/Agentes para manter TZ/`days` idênticos.
  const buildPeriodQuery = (): string => {
    if (selectedPeriod === 'custom' && customStartDate && customEndDate) {
      return `start_date=${customStartDate}&end_date=${customEndDate}`;
    }
    if (selectedPeriod === 'today') {
      const today = getLocalDateString(new Date());
      return `start_date=${today}&end_date=${today}`;
    }
    return `days=${selectedPeriod}`;
  };

  // `true` quando o período ainda não é resolvível (custom sem datas) → não buscar.
  const periodIncomplete = selectedPeriod === 'custom' && !(customStartDate && customEndDate);

  // ----- fetch (mirror billing 199-236) -----
  useEffect(() => {
    if (periodIncomplete) return;

    let cancelled = false;

    const buildQuery = buildPeriodQuery;

    const run = async () => {
      setLoading(true);
      setAccessError(null);
      try {
        const q = buildQuery();
        const [summaryRes, seriesRes] = await Promise.all([
          fetch(`/api/admin/metrics/summary?${q}`),
          fetch(`/api/admin/metrics/timeseries?${q}`),
        ]);

        if (summaryRes.status === 403 || seriesRes.status === 403) {
          if (!cancelled) {
            setAccessError('Apenas o Owner pode acessar Métricas.');
            setSummary(EMPTY_SUMMARY);
            setSeries([]);
          }
          return;
        }
        if (summaryRes.status === 400 || seriesRes.status === 400) {
          if (!cancelled) {
            setAccessError('company_id obrigatório para master.');
            setSummary(EMPTY_SUMMARY);
            setSeries([]);
          }
          return;
        }

        if (summaryRes.ok && !cancelled) {
          setSummary((await summaryRes.json()) as MetricsSummary);
        }
        if (seriesRes.ok && !cancelled) {
          const data = await seriesRes.json();
          // tolerate {daily:[...]} or bare array
          setSeries(Array.isArray(data) ? data : (data?.daily ?? []));
        }
      } catch (err) {
        console.error('Error fetching metrics:', err);
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    run();
    return () => {
      cancelled = true;
    };
  }, [selectedPeriod, customStartDate, customEndDate]);

  // ----- fetch LAZY: Atendimentos (só quando a aba está ativa) -----
  // Padrão novo: gate por activeTab. Re-busca quando o período muda E a aba é a ativa.
  useEffect(() => {
    if (activeTab !== 'atendimentos' || periodIncomplete) return;

    let cancelled = false;
    const run = async () => {
      setAttendanceLoading(true);
      setAttendanceError(null);
      try {
        const res = await fetch(`/api/admin/metrics/attendance?${buildPeriodQuery()}`);
        if (res.status === 403) {
          if (!cancelled) {
            setAttendanceError('Apenas o Owner pode acessar Métricas.');
            setAttendance(EMPTY_ATTENDANCE);
          }
          return;
        }
        if (res.ok && !cancelled) {
          const data = (await res.json()) as Partial<AttendanceData>;
          setAttendance({
            by_admin: Array.isArray(data.by_admin) ? data.by_admin : [],
            sla: {
              first_response_pct: data.sla?.first_response_pct ?? null,
              resolution_pct: data.sla?.resolution_pct ?? null,
              breached_count: data.sla?.breached_count ?? 0,
              breaches: Array.isArray(data.sla?.breaches) ? data.sla!.breaches : [],
            },
          });
        }
      } catch (err) {
        console.error('Error fetching attendance metrics:', err);
        if (!cancelled) setAttendanceError('Erro ao carregar atendimentos.');
      } finally {
        if (!cancelled) setAttendanceLoading(false);
      }
    };

    run();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, selectedPeriod, customStartDate, customEndDate]);

  // ----- fetch LAZY: Agentes (só quando a aba está ativa) -----
  useEffect(() => {
    if (activeTab !== 'agentes' || periodIncomplete) return;

    let cancelled = false;
    const run = async () => {
      setAgentsLoading(true);
      setAgentsError(null);
      try {
        const res = await fetch(`/api/admin/metrics/by-agent?${buildPeriodQuery()}`);
        if (res.status === 403) {
          if (!cancelled) {
            setAgentsError('Apenas o Owner pode acessar Métricas.');
            setAgents([]);
          }
          return;
        }
        if (res.ok && !cancelled) {
          const data = await res.json();
          const list = Array.isArray(data) ? data : (data?.by_agent ?? []);
          // bigint do Postgres pode vir como string → normaliza p/ number.
          setAgents(
            (list as AgentRow[]).map((row) => ({
              agent_id: row.agent_id ?? null,
              agent_name: row.agent_name ?? null,
              messages: Number(row.messages) || 0,
              conversations: Number(row.conversations) || 0,
            })),
          );
        }
      } catch (err) {
        console.error('Error fetching agent metrics:', err);
        if (!cancelled) setAgentsError('Erro ao carregar agentes.');
      } finally {
        if (!cancelled) setAgentsLoading(false);
      }
    };

    run();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, selectedPeriod, customStartDate, customEndDate]);

  return (
    <PageShell size="default">
      <PageHeader>
        <div>
          <PageTitle className="flex items-center gap-2">
            <BarChart3 className="w-8 h-8" /> Métricas
          </PageTitle>
          <PageDescription>
            Acompanhe conversas, mensagens, leads e consumo da sua operação
          </PageDescription>
        </div>
      </PageHeader>

      <Tabs value={activeTab} onValueChange={setActiveTab} className="space-y-6">
        {/* Seletor de período — compartilhado pelas 3 abas (fora de qualquer TabsContent). */}
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <TabsList className="bg-card border border-border p-1">
            <TabsTrigger
              value="geral"
              className="data-[state=active]:bg-primary data-[state=active]:text-primary-foreground flex items-center gap-2"
            >
              <BarChart3 className="w-4 h-4" /> Geral
            </TabsTrigger>
            <TabsTrigger
              value="atendimentos"
              className="data-[state=active]:bg-primary data-[state=active]:text-primary-foreground flex items-center gap-2"
            >
              <Headphones className="w-4 h-4" /> Atendimentos
            </TabsTrigger>
            <TabsTrigger
              value="agentes"
              className="data-[state=active]:bg-primary data-[state=active]:text-primary-foreground flex items-center gap-2"
            >
              <Bot className="w-4 h-4" /> Agentes
            </TabsTrigger>
          </TabsList>

          {/* Period Selector (copy billing 603-641: native <select>, NO chips, NO timezone) */}
          <div className="flex flex-wrap items-center gap-2">
            <Calendar className="w-4 h-4 text-muted-foreground" />
            <select
              value={selectedPeriod}
              onChange={(e) => setSelectedPeriod(e.target.value)}
              className="bg-card border border-border rounded-lg px-3 py-2 text-sm text-foreground focus:outline-none focus:border-primary"
            >
              <option value="today">Hoje</option>
              <option value="7">Últimos 7 dias</option>
              <option value="30">Últimos 30 dias</option>
              <option value="90">Últimos 3 meses</option>
              <option value="custom">Personalizado</option>
            </select>

            {selectedPeriod === 'custom' && (
              <>
                <input
                  type="date"
                  value={customStartDate}
                  onChange={(e) => setCustomStartDate(e.target.value)}
                  className="bg-card border border-border rounded-lg px-3 py-2 text-sm text-foreground focus:outline-none focus:border-primary"
                />
                <span className="text-muted-foreground">até</span>
                <input
                  type="date"
                  value={customEndDate}
                  onChange={(e) => setCustomEndDate(e.target.value)}
                  className="bg-card border border-border rounded-lg px-3 py-2 text-sm text-foreground focus:outline-none focus:border-primary"
                />
              </>
            )}
          </div>
        </div>

        <TabsContent value="geral" className="space-y-6">
          {/* h2 específico da Geral — desacoplado do seletor de período (que subiu). */}
          <h2 className="text-xl font-bold text-foreground flex items-center gap-2">
            <BarChart3 className="w-5 h-5 text-accent" />
            Conversas e Mensagens ao Longo do Tempo
          </h2>

          {accessError && (
            <div className="rounded-xl border border-warning/30 bg-warning/10 p-4 text-sm text-warning">
              {accessError}
            </div>
          )}

          {/* 6 cards: grid-cols-1 md:grid-cols-2 lg:grid-cols-3 (2×3) */}
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            <MetricCard
              label="Total de Conversas"
              value={formatNumber(summary.total_conversations)}
              description={getPeriodLabel()}
              icon={MessageSquare}
              tone="brand"
            />
            <MetricCard
              label="Total de Mensagens"
              value={formatNumber(summary.total_messages)}
              description="mensagens trocadas"
              icon={BarChart3}
              tone="info"
            />
            <MetricCard
              label="Novas Conversas"
              value={formatNumber(summary.new_conversations)}
              description="iniciadas no período"
              icon={UserPlus}
              tone="success"
            />
            <MetricCard
              label="Conversas Existentes"
              value={formatNumber(summary.existing_conversations)}
              description="retomadas no período"
              icon={Repeat}
              tone="neutral"
            />
            <MetricCard
              label="Leads Gerados"
              value={formatNumber(summary.leads_generated)}
              description="contatos com e-mail/telefone no período"
              icon={Users}
              tone="warning"
            />
            <MetricCard
              label="Créditos Consumidos"
              value={formatNumber(brlToCredits(summary.credits_consumed_brl))}
              description={getPeriodLabel()}
              icon={Coins}
              tone="brand"
            />
          </div>

          {/* Dual-area chart (mirror billing 668-717: two <Area> + two <linearGradient>) */}
          <div className="bg-card border border-border rounded-xl p-6">
            <h3 className="text-lg font-bold text-foreground mb-4">
              Conversas e Mensagens ao Longo do Tempo
            </h3>
            <div className="h-[250px]">
              {loading ? (
                <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
                  Carregando…
                </div>
              ) : series.length === 0 ? (
                <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
                  Sem dados no período selecionado
                </div>
              ) : (
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={series}>
                    <defs>
                      <linearGradient id="colorConversations" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="hsl(var(--primary))" stopOpacity={0.22} />
                        <stop offset="95%" stopColor="hsl(var(--primary))" stopOpacity={0} />
                      </linearGradient>
                      <linearGradient id="colorMessages" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="hsl(var(--chart-2))" stopOpacity={0.22} />
                        <stop offset="95%" stopColor="hsl(var(--chart-2))" stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
                    <XAxis
                      dataKey="date"
                      tickFormatter={formatShortDate}
                      stroke="hsl(var(--muted-foreground))"
                      fontSize={12}
                    />
                    <YAxis
                      stroke="hsl(var(--muted-foreground))"
                      fontSize={12}
                      allowDecimals={false}
                    />
                    <Tooltip
                      contentStyle={{
                        backgroundColor: 'hsl(var(--popover))',
                        border: '1px solid hsl(var(--border))',
                        borderRadius: '8px',
                      }}
                      labelFormatter={(label) => formatDate(label as string)}
                      formatter={(value: number, name: string) => [
                        formatNumber(value),
                        name === 'conversations' ? 'Conversas' : 'Mensagens',
                      ]}
                    />
                    <Area
                      type="monotone"
                      dataKey="conversations"
                      stroke="hsl(var(--primary))"
                      strokeWidth={2}
                      fillOpacity={1}
                      fill="url(#colorConversations)"
                    />
                    <Area
                      type="monotone"
                      dataKey="messages"
                      stroke="hsl(var(--chart-2))"
                      strokeWidth={2}
                      fillOpacity={1}
                      fill="url(#colorMessages)"
                    />
                  </AreaChart>
                </ResponsiveContainer>
              )}
            </div>
          </div>
        </TabsContent>

        {/* ===================== ABA ATENDIMENTOS (SPEC §4) ===================== */}
        <TabsContent value="atendimentos" className="space-y-6">
          {attendanceError && (
            <div className="rounded-xl border border-warning/30 bg-warning/10 p-4 text-sm text-warning">
              {attendanceError}
            </div>
          )}

          {/* Seção B — SLA: cards no topo (SPEC §4.2). */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {attendanceLoading ? (
              <>
                <Skeleton className="h-[116px]" />
                <Skeleton className="h-[116px]" />
                <Skeleton className="h-[116px]" />
              </>
            ) : (
              <>
                <MetricCard
                  label="1ª resposta no prazo"
                  value={formatPct(attendance.sla.first_response_pct)}
                  description="SLA de primeira resposta cumprido"
                  icon={Timer}
                  tone="info"
                />
                <MetricCard
                  label="Resolução no prazo"
                  value={formatPct(attendance.sla.resolution_pct)}
                  description="SLA de resolução cumprido"
                  icon={CheckCircle2}
                  tone="success"
                />
                <MetricCard
                  label="SLAs não cumpridos"
                  value={formatNumber(attendance.sla.breached_count)}
                  description={getPeriodLabel()}
                  icon={AlertTriangle}
                  tone="danger"
                />
              </>
            )}
          </div>

          {/* Seção A — Ranking por admin (SPEC §4.1). */}
          <div className="bg-card border border-border rounded-xl p-6">
            <div className="mb-4 flex items-center gap-2">
              <Headphones className="w-5 h-5 text-accent" />
              <h2 className="text-lg font-bold text-foreground">Atendimentos por responsável</h2>
            </div>
            {attendanceLoading ? (
              <div className="space-y-3">
                <Skeleton className="h-12" />
                <Skeleton className="h-12" />
                <Skeleton className="h-12" />
              </div>
            ) : attendance.by_admin.length === 0 ? (
              <div className="flex flex-col items-center justify-center gap-2 py-10 text-center">
                <Headphones className="w-8 h-8 text-muted-foreground/50" />
                <p className="text-sm text-muted-foreground">Nenhum atendimento no período</p>
              </div>
            ) : (
              <RankingBars<AttendanceAdminRow>
                rows={attendance.by_admin}
                getKey={(row, i) => row.user_id ?? `admin-${i}`}
                getLabel={(row) => (
                  <span className="flex items-center gap-2">
                    {adminDisplayName(row)}
                    <span
                      className={
                        row.is_owner
                          ? 'rounded border border-primary/20 bg-brand-muted px-1.5 py-0.5 text-[10px] font-semibold uppercase text-primary'
                          : 'rounded border border-border bg-muted px-1.5 py-0.5 text-[10px] font-semibold uppercase text-muted-foreground'
                      }
                    >
                      {row.is_owner ? 'Owner' : 'Admin'}
                    </span>
                  </span>
                )}
                getValue={(row) => row.taken}
                valueSuffix="assumidos"
                columns={[
                  { header: 'Resolvidos', render: (row) => formatNumber(row.resolved) },
                ]}
              />
            )}
          </div>

          {/* Seção B — SLA: lista de não cumpridos (SPEC §4.2, read-only). */}
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <AlertTriangle className="w-5 h-5 text-danger" />
              <h2 className="text-lg font-bold text-foreground">SLAs não cumpridos</h2>
            </div>
            {attendanceLoading ? (
              <div className="space-y-2">
                <Skeleton className="h-12" />
                <Skeleton className="h-12" />
                <Skeleton className="h-12" />
              </div>
            ) : attendance.sla.breaches.length === 0 ? (
              <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-border bg-card py-10 text-center">
                <CheckCircle2 className="w-8 h-8 text-success/60" />
                <p className="text-sm text-muted-foreground">Nenhum SLA não cumprido no período</p>
              </div>
            ) : (
              <BreachList breaches={attendance.sla.breaches} />
            )}
          </div>
        </TabsContent>

        {/* ===================== ABA AGENTES (SPEC §5) ===================== */}
        <TabsContent value="agentes" className="space-y-6">
          {agentsError && (
            <div className="rounded-xl border border-warning/30 bg-warning/10 p-4 text-sm text-warning">
              {agentsError}
            </div>
          )}

          <div className="bg-card border border-border rounded-xl p-6">
            <div className="mb-4 flex items-center gap-2">
              <Bot className="w-5 h-5 text-accent" />
              <h2 className="text-lg font-bold text-foreground">Atividade por agente</h2>
            </div>
            {agentsLoading ? (
              <div className="space-y-3">
                <Skeleton className="h-12" />
                <Skeleton className="h-12" />
                <Skeleton className="h-12" />
                <Skeleton className="h-12" />
              </div>
            ) : agents.length === 0 ? (
              <div className="flex flex-col items-center justify-center gap-2 py-10 text-center">
                <Bot className="w-8 h-8 text-muted-foreground/50" />
                <p className="text-sm text-muted-foreground">Nenhuma atividade de agente no período</p>
              </div>
            ) : (
              <RankingBars<AgentRow>
                rows={agents}
                getKey={(row, i) => row.agent_id ?? `agent-${i}`}
                getLabel={(row) => row.agent_name?.trim() || 'Agente sem nome'}
                getValue={(row) => row.messages}
                valueSuffix="msgs"
                columns={[
                  { header: 'Conversas', render: (row) => formatNumber(row.conversations) },
                ]}
              />
            )}
          </div>
        </TabsContent>
      </Tabs>
    </PageShell>
  );
}
