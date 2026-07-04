'use client';

import { useEffect, useState } from 'react';
import {
  DollarSign,
  Activity,
  Building2,
  Cpu,
  Calendar as CalendarIcon,
  RefreshCw,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Calendar } from '@/components/ui/calendar';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { format } from 'date-fns';
import { ptBR } from 'date-fns/locale';
import { InlineNotice } from '@/components/ui/feedback-state';
import { MetricCard } from '@/components/ui/metric-card';
import {
  PageActions,
  PageDescription,
  PageHeader,
  PageShell,
  PageTitle,
} from '@/components/ui/page-shell';

interface UsageReport {
  company_name: string;
  service_type: string;
  model_name: string;
  total_calls: number;
  total_input: number;
  total_output: number;
  total_cost: number;
}

interface CompanyTotal {
  company_id: string;
  company_name: string;
  total_calls: number;
  total_input: number;
  total_output: number;
  total_cost: number;
}

type DateRange = 'today' | '7d' | '30d' | 'month' | 'all' | 'custom';

interface CustomDateRange {
  from: Date | undefined;
  to: Date | undefined;
}

export default function CostsPage() {
  const [report, setReport] = useState<UsageReport[]>([]);
  const [companyTotals, setCompanyTotals] = useState<CompanyTotal[]>([]);
  const [loading, setLoading] = useState(true);
  const [dateRange, setDateRange] = useState<DateRange>('today');
  const [error, setError] = useState<string | null>(null);
  const [customRange, setCustomRange] = useState<CustomDateRange>({
    from: undefined,
    to: undefined,
  });
  const [selectedCompany, setSelectedCompany] = useState<string>('all');
  const [showDatePicker, setShowDatePicker] = useState(false);

  const getDateRange = (range: DateRange): { start: string; end: string } => {
    const now = new Date();
    const end = now.toISOString();
    let start: Date;

    switch (range) {
      case 'today':
        start = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0, 0);
        break;
      case '7d':
        start = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000);
        break;
      case '30d':
        start = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000);
        break;
      case 'month':
        start = new Date(now.getFullYear(), now.getMonth(), 1);
        break;
      case 'all':
        start = new Date('2024-01-01');
        break;
      case 'custom':
        if (customRange.from && customRange.to) {
          return {
            start: customRange.from.toISOString(),
            end: new Date(customRange.to.getTime() + 24 * 60 * 60 * 1000 - 1).toISOString(),
          };
        }
        start = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0, 0);
        break;
      default:
        start = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0, 0);
    }

    return { start: start.toISOString(), end };
  };

  const fetchData = async () => {
    setLoading(true);
    setError(null);

    try {
      const { start, end } = getDateRange(dateRange);

      let url = `/api/admin/costs?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`;
      if (selectedCompany !== 'all') {
        url += `&company_id=${encodeURIComponent(selectedCompany)}`;
      }

      const response = await fetch(url, {
        credentials: 'include',
      });

      if (!response.ok) {
        throw new Error('Failed to fetch costs data');
      }

      const data = await response.json();

      if (data.error) {
        if (data.error.includes('does not exist')) {
          setError(
            `Função RPC não encontrada. Execute a migração 016 no Supabase SQL Editor. Erro: ${data.error}`,
          );
        } else {
          setError(`Erro ao chamar RPC: ${data.error}`);
        }
      }

      setReport(data.report || []);
      setCompanyTotals(data.companyTotals || []);
    } catch (err) {
      console.error('Error fetching data:', err);
      setError('Erro ao carregar dados');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
  }, [dateRange, customRange, selectedCompany]);

  // Calculate totals
  const totalCost = report.reduce((sum, r) => sum + (r.total_cost || 0), 0);
  const totalCalls = report.reduce((sum, r) => sum + (r.total_calls || 0), 0);
  const totalTokens = report.reduce(
    (sum, r) => sum + (r.total_input || 0) + (r.total_output || 0),
    0,
  );

  // Group by service
  const serviceBreakdown = report.reduce(
    (acc, r) => {
      const service = r.service_type || 'unknown';
      if (!acc[service]) {
        acc[service] = { calls: 0, cost: 0 };
      }
      acc[service].calls += r.total_calls || 0;
      acc[service].cost += r.total_cost || 0;
      return acc;
    },
    {} as Record<string, { calls: number; cost: number }>,
  );

  const topCompany =
    companyTotals.length > 0
      ? companyTotals.reduce(
          (max, c) => (c.total_cost > max.total_cost ? c : max),
          companyTotals[0],
        )
      : null;

  const formatCurrency = (value: number) => {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: 4,
      maximumFractionDigits: 4,
    }).format(value);
  };

  const formatNumber = (value: number) => {
    return new Intl.NumberFormat('pt-BR').format(value);
  };

  return (
    <PageShell>
      <PageHeader>
        <div>
          <PageTitle>FinOps Dashboard</PageTitle>
          <PageDescription>Monitoramento de custos e uso de tokens OpenAI</PageDescription>
        </div>

        <PageActions className="gap-4">
          {/* Company Filter */}
          <Select value={selectedCompany} onValueChange={setSelectedCompany}>
            <SelectTrigger className="w-[200px] bg-background border-input text-foreground">
              <Building2 className="w-4 h-4 mr-2 text-muted-foreground" />
              <SelectValue placeholder="Todas empresas" />
            </SelectTrigger>
            <SelectContent className="bg-background border-border">
              <SelectItem value="all" className="text-foreground hover:bg-muted">
                Todas empresas
              </SelectItem>
              {companyTotals.map((company) => (
                <SelectItem
                  key={company.company_id}
                  value={company.company_id}
                  className="text-foreground hover:bg-muted"
                >
                  {company.company_name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          {/* Date Range Selector */}
          <div className="flex bg-muted rounded-lg p-1">
            {[
              { value: 'today', label: 'Hoje' },
              { value: '7d', label: '7 dias' },
              { value: '30d', label: '30 dias' },
              { value: 'month', label: 'Este mês' },
              { value: 'all', label: 'Tudo' },
            ].map((option) => (
              <button
                key={option.value}
                onClick={() => {
                  setDateRange(option.value as DateRange);
                  setShowDatePicker(false);
                }}
                className={`px-4 py-2 rounded-md text-sm transition-colors ${
                  dateRange === option.value
                    ? 'bg-background shadow-sm text-foreground'
                    : 'text-muted-foreground hover:text-foreground'
                }`}
              >
                {option.label}
              </button>
            ))}

            {/* Custom Date Range Picker */}
            <Popover open={showDatePicker} onOpenChange={setShowDatePicker}>
              <PopoverTrigger asChild>
                <button
                  onClick={() => {
                    setDateRange('custom');
                    setShowDatePicker(true);
                  }}
                  className={`px-4 py-2 rounded-md text-sm transition-colors flex items-center gap-2 ${
                    dateRange === 'custom'
                      ? 'bg-background shadow-sm text-foreground'
                      : 'text-muted-foreground hover:text-foreground'
                  }`}
                >
                  <CalendarIcon className="w-4 h-4" />
                  {dateRange === 'custom' && customRange.from && customRange.to
                    ? `${format(customRange.from, 'dd/MM', { locale: ptBR })} - ${format(customRange.to, 'dd/MM', { locale: ptBR })}`
                    : 'Período'}
                </button>
              </PopoverTrigger>
              <PopoverContent className="w-auto p-0 bg-popover border-border" align="end">
                <Calendar
                  mode="range"
                  defaultMonth={customRange.from}
                  selected={{ from: customRange.from, to: customRange.to }}
                  onSelect={(range) => {
                    setCustomRange({ from: range?.from, to: range?.to });
                    if (range?.from && range?.to) {
                      setShowDatePicker(false);
                    }
                  }}
                  numberOfMonths={2}
                  locale={ptBR}
                  className="text-foreground"
                />
              </PopoverContent>
            </Popover>
          </div>

          <Button onClick={fetchData} variant="outline">
            <RefreshCw className={`w-4 h-4 mr-2 ${loading ? 'animate-spin' : ''}`} />
            Atualizar
          </Button>
        </PageActions>
      </PageHeader>

      {error ? (
        <InlineNotice tone="warning" className="font-normal">
          <p className="text-warning">{error}</p>
          <p className="text-muted-foreground text-sm mt-2">
            Execute:{' '}
            <code className="bg-muted px-2 py-1 rounded">
              psql -f backend/migrations/016_create_token_usage_logs.sql
            </code>
          </p>
        </InlineNotice>
      ) : (
        <>
          {/* KPI Cards */}
          <div className="grid grid-cols-1 md:grid-cols-4 gap-6 mb-8">
            <MetricCard
              label="Custo Total"
              value={formatCurrency(totalCost)}
              icon={DollarSign}
              tone="success"
            />
            <MetricCard
              label="Total de Chamadas"
              value={formatNumber(totalCalls)}
              icon={Activity}
              tone="brand"
            />
            <MetricCard
              label="Tokens Processados"
              value={formatNumber(totalTokens)}
              icon={Cpu}
              tone="info"
            />
            <MetricCard
              label="Maior Consumidor"
              value={topCompany?.company_name || 'N/A'}
              description={topCompany ? formatCurrency(topCompany.total_cost) : undefined}
              icon={Building2}
              tone="warning"
            />
          </div>

          {/* Service Breakdown */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
            <div className="bg-card border border-border rounded-xl p-6">
              <h3 className="text-lg font-semibold text-foreground mb-4">Custo por Serviço</h3>
              <div className="space-y-3">
                {Object.entries(serviceBreakdown).map(([service, data]) => (
                  <div key={service} className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <div
                        className={`w-3 h-3 rounded-full ${
                          service === 'chat'
                            ? 'bg-primary'
                            : service === 'memory'
                              ? 'bg-success'
                              : service === 'ingestion'
                                ? 'bg-accent'
                                : service === 'benchmark'
                                  ? 'bg-warning'
                                  : service === 'audio'
                                    ? 'bg-accent'
                                    : service === 'embedding'
                                      ? 'bg-info'
                                      : 'bg-muted-foreground'
                        }`}
                      />
                      <span className="text-muted-foreground capitalize">{service}</span>
                    </div>
                    <div className="text-right">
                      <p className="text-foreground font-medium">{formatCurrency(data.cost)}</p>
                      <p className="text-muted-foreground text-xs">
                        {formatNumber(data.calls)} chamadas
                      </p>
                    </div>
                  </div>
                ))}
                {Object.keys(serviceBreakdown).length === 0 && (
                  <p className="text-muted-foreground text-center py-4">Nenhum dado ainda</p>
                )}
              </div>
            </div>

            <div className="bg-card border border-border rounded-xl p-6">
              <h3 className="text-lg font-semibold text-foreground mb-4">Custo por Empresa</h3>
              <div className="space-y-3 max-h-64 overflow-y-auto">
                {companyTotals.map((company, idx) => (
                  <div
                    key={company.company_id || idx}
                    className="flex items-center justify-between"
                  >
                    <span className="text-muted-foreground">{company.company_name}</span>
                    <div className="text-right">
                      <p className="text-foreground font-medium">
                        {formatCurrency(company.total_cost)}
                      </p>
                      <p className="text-muted-foreground text-xs">
                        {formatNumber(company.total_calls)} chamadas
                      </p>
                    </div>
                  </div>
                ))}
                {companyTotals.length === 0 && (
                  <p className="text-muted-foreground text-center py-4">Nenhum dado ainda</p>
                )}
              </div>
            </div>
          </div>

          {/* Detailed Table */}
          <div className="bg-card border border-border rounded-xl overflow-hidden">
            <div className="p-6 border-b border-border">
              <h3 className="text-lg font-semibold text-foreground">Detalhamento por Modelo</h3>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead className="bg-muted/50">
                  <tr>
                    <th className="px-6 py-4 text-left text-xs font-medium text-muted-foreground uppercase">
                      Empresa
                    </th>
                    <th className="px-6 py-4 text-left text-xs font-medium text-muted-foreground uppercase">
                      Serviço
                    </th>
                    <th className="px-6 py-4 text-left text-xs font-medium text-muted-foreground uppercase">
                      Modelo
                    </th>
                    <th className="px-6 py-4 text-right text-xs font-medium text-muted-foreground uppercase">
                      Chamadas
                    </th>
                    <th className="px-6 py-4 text-right text-xs font-medium text-muted-foreground uppercase">
                      Input
                    </th>
                    <th className="px-6 py-4 text-right text-xs font-medium text-muted-foreground uppercase">
                      Output
                    </th>
                    <th className="px-6 py-4 text-right text-xs font-medium text-muted-foreground uppercase">
                      Custo
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {report.map((row, idx) => (
                    <tr key={idx} className="hover:bg-muted/50">
                      <td className="px-6 py-4 text-muted-foreground">{row.company_name}</td>
                      <td className="px-6 py-4">
                        <span
                          className={`px-2 py-1 rounded text-xs font-medium ${
                            row.service_type === 'chat'
                              ? 'bg-primary/10 text-primary'
                              : row.service_type === 'memory'
                                ? 'bg-success/10 text-success'
                                : row.service_type === 'ingestion'
                                  ? 'bg-accent/10 text-accent'
                                  : row.service_type === 'benchmark'
                                    ? 'bg-warning/10 text-warning'
                                    : row.service_type === 'audio'
                                      ? 'bg-accent/10 text-accent'
                                      : row.service_type === 'embedding'
                                        ? 'bg-info/20 text-info'
                                        : 'bg-muted text-muted-foreground'
                          }`}
                        >
                          {row.service_type}
                        </span>
                      </td>
                      <td className="px-6 py-4 text-muted-foreground font-mono text-sm">
                        {row.model_name}
                      </td>
                      <td className="px-6 py-4 text-right text-muted-foreground">
                        {formatNumber(row.total_calls)}
                      </td>
                      <td className="px-6 py-4 text-right text-muted-foreground">
                        {formatNumber(row.total_input)}
                      </td>
                      <td className="px-6 py-4 text-right text-muted-foreground">
                        {formatNumber(row.total_output)}
                      </td>
                      <td className="px-6 py-4 text-right text-success font-medium">
                        {formatCurrency(row.total_cost)}
                      </td>
                    </tr>
                  ))}
                  {report.length === 0 && (
                    <tr>
                      <td colSpan={7} className="px-6 py-8 text-center text-muted-foreground">
                        Nenhum dado de uso encontrado para o período selecionado
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </PageShell>
  );
}
