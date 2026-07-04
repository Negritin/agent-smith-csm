// app/admin/conversation-logs/page.tsx

'use client';

import React, { useEffect, useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import {
  MessageSquare,
  Clock,
  Database,
  Building2,
  Zap,
  Sparkles, // Para HyDE
  Globe, // Para Web
  HelpCircle,
  Search, // Para busca
  Bot, // Para agente
  User, // Para usuário
} from 'lucide-react';
import { Input } from '@/components/ui/input';
import type { Company } from '@/lib/types';
import { MetricCard } from '@/components/ui/metric-card';
import {
  PageActions,
  PageDescription,
  PageHeader,
  PageShell,
  PageTitle,
} from '@/components/ui/page-shell';

interface ConversationLog {
  id: string;
  company_id: string;
  user_id: string;
  session_id: string;
  user_question: string;
  assistant_response: string;
  rag_chunks: Array<{
    chunk_id: string;
    score: number;
    content_preview?: string;
    content?: string;
    metadata?: {
      document_name?: string;
      [key: string]: any;
    };
    used_in_context?: boolean;
  }> | null;
  rag_chunks_count: number;
  tokens_input: number | null;
  tokens_output: number | null;
  tokens_total: number | null;
  llm_provider: string;
  llm_model: string;
  llm_temperature: number;
  response_time_ms: number | null;
  rag_search_time_ms: number | null;
  search_strategy: string | null; // 'standard', 'hyde', 'standard_fallback', 'web', 'hybrid_direct', 'hyde_hybrid'
  retrieval_score: number | null; // 0.00 to 1.00
  timestamp: string;
  created_at: string;
  status: string;
  error_message: string | null;
  agent_id: string | null;
}

interface Agent {
  id: string;
  name: string;
  company_id: string;
}

interface UserInfo {
  id: string;
  email: string;
  first_name: string | null;
  last_name: string | null;
}

export default function ConversationLogsPage() {
  const [logs, setLogs] = useState<ConversationLog[]>([]);
  const [companies, setCompanies] = useState<Company[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [users, setUsers] = useState<UserInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedCompany, setSelectedCompany] = useState<string>('all');
  const [companySearch, setCompanySearch] = useState('');
  const [expandedRow, setExpandedRow] = useState<string | null>(null);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [selectedLog, setSelectedLog] = useState<ConversationLog | null>(null);

  // Pagination
  const [currentPage, setCurrentPage] = useState(1);
  const itemsPerPage = 15;

  useEffect(() => {
    loadCompanies();
    loadAgents();
    loadUsers();
  }, []);

  useEffect(() => {
    loadLogs();
  }, [selectedCompany]);

  const loadCompanies = async () => {
    try {
      const response = await fetch('/api/admin/conversation-logs/data');
      if (!response.ok) throw new Error('Failed to fetch data');

      const data = await response.json();
      setCompanies((data.companies || []) as Company[]);
      setAgents(data.agents || []);
      setUsers(data.users || []);
    } catch (error) {
      console.error('Error loading data:', error);
    }
  };

  /* loadAgents and loadUsers now combined into loadCompanies */

  const loadAgents = async () => {};
  const loadUsers = async () => {};

  const loadLogs = async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (selectedCompany !== 'all') {
        params.append('company_id', selectedCompany);
      }

      const response = await fetch(`/api/admin/conversation-logs?${params}`);
      if (!response.ok) throw new Error('Failed to fetch logs');

      const data = await response.json();
      setLogs(data.logs || []);
    } catch (error) {
      console.error('Error loading logs:', error);
    } finally {
      setLoading(false);
    }
  };

  const getProviderBadge = (provider: string) => {
    const colors = {
      openai: 'bg-success',
      anthropic: 'bg-warning',
      google: 'bg-primary',
    };
    return colors[provider as keyof typeof colors] || 'bg-muted-foreground';
  };

  const formatDate = (dateString: string) => {
    const date = new Date(dateString);
    return date.toLocaleString('pt-BR', {
      day: '2-digit',
      month: '2-digit',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  // NOVA LÓGICA DE BADGES (Multi-icones)
  const getSearchStrategyBadges = (strategy: string | null, score: number | null) => {
    if (!strategy) return <span className="text-xs text-muted-foreground">-</span>;

    const badges = [];
    const s = strategy.toLowerCase();

    // Verifica quais estratégias estão presentes na string (ex: 'hyde_hybrid')
    const hasWeb = s.includes('web');
    const hasHyde = s.includes('hyde');
    const hasHybrid = s.includes('hybrid');
    const hasStandard = s.includes('standard');

    // Web Search
    if (hasWeb) {
      badges.push({
        icon: <Globe className="w-3 h-3" />,
        label: 'Web',
        className: 'bg-primary/10 text-primary border-primary/20',
      });
    }

    // HyDE
    if (hasHyde) {
      badges.push({
        icon: <Sparkles className="w-3 h-3" />,
        label: 'HyDE',
        className: 'bg-accent/10 text-accent border-accent/25',
      });
    }

    // Hybrid (Dense + Sparse)
    if (hasHybrid) {
      badges.push({
        icon: <Zap className="w-3 h-3" />,
        label: 'Hybrid',
        className: 'bg-primary text-primary-foreground border-transparent',
      });
    }

    // Fallback ou Standard puro
    if (!hasWeb && !hasHyde && !hasHybrid) {
      if (hasStandard) {
        badges.push({
          icon: <Database className="w-3 h-3" />,
          label: 'Standard',
          className: 'bg-success/10 text-success border-success/20',
        });
      } else {
        // Outro desconhecido
        badges.push({
          icon: <HelpCircle className="w-3 h-3" />,
          label: strategy,
          className: 'bg-muted text-muted-foreground border-border',
        });
      }
    }

    return (
      <div className="flex flex-col gap-1.5 items-start">
        <div className="flex flex-wrap gap-1">
          {badges.map((badge, idx) => (
            <Badge
              key={idx}
              variant="outline"
              className={`flex items-center gap-1 px-1.5 py-0.5 text-[10px] font-medium border ${badge.className}`}
            >
              {badge.icon}
              {badge.label}
            </Badge>
          ))}
        </div>
        {score !== null && score !== undefined && (
          <span className="text-[10px] text-muted-foreground ml-0.5">
            Avg Score: {score.toFixed(3)}
          </span>
        )}
      </div>
    );
  };

  const toggleRow = (logId: string) => {
    setExpandedRow(expandedRow === logId ? null : logId);
  };

  const openDetails = (log: ConversationLog) => {
    setSelectedLog(log);
    setDetailsOpen(true);
  };

  const getCompanyName = (companyId: string) => {
    const company = companies.find((c) => c.id === companyId);
    return company?.company_name || companyId;
  };

  const getAgentName = (agentId: string | null) => {
    if (!agentId) return null;
    const agent = agents.find((a) => a.id === agentId);
    return agent?.name || null;
  };

  const getUserName = (userId: string) => {
    const user = users.find((u) => u.id === userId);
    if (user?.first_name) {
      return `${user.first_name}${user.last_name ? ' ' + user.last_name : ''}`;
    }
    return user?.email?.split('@')[0] || userId.slice(0, 8);
  };

  // Filtrar empresas baseado na busca
  const filteredCompanies = companies.filter((c) =>
    c.company_name.toLowerCase().includes(companySearch.toLowerCase()),
  );

  // Pagination logic
  const totalPages = Math.ceil(logs.length / itemsPerPage);
  const paginatedLogs = logs.slice((currentPage - 1) * itemsPerPage, currentPage * itemsPerPage);

  return (
    <PageShell>
      <PageHeader>
        <div>
          <PageTitle className="flex items-center gap-3">
            <MessageSquare className="h-8 w-8 text-primary" />
            Logs de Conversas
          </PageTitle>
          <PageDescription>Histórico detalhado de interações com o agente</PageDescription>
        </div>

        <PageActions className="gap-4">
          <div className="relative">
            <div className="relative">
              <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input
                placeholder="Buscar empresa..."
                value={companySearch}
                onChange={(e) => setCompanySearch(e.target.value)}
                className="w-[280px] pl-10 bg-card border-border text-foreground"
              />
            </div>
            {companySearch && filteredCompanies.length > 0 && (
              <div className="absolute z-50 w-full mt-1 bg-card border border-border rounded-md shadow-lg max-h-60 overflow-auto">
                <button
                  onClick={() => {
                    setSelectedCompany('all');
                    setCompanySearch('');
                  }}
                  className="w-full px-3 py-2 text-left text-sm text-muted-foreground hover:bg-muted transition-colors"
                >
                  Todas as empresas
                </button>
                {filteredCompanies.map((company) => (
                  <button
                    key={company.id}
                    onClick={() => {
                      setSelectedCompany(company.id);
                      setCompanySearch(company.company_name);
                    }}
                    className="w-full px-3 py-2 text-left text-sm text-foreground hover:bg-muted transition-colors"
                  >
                    {company.company_name}
                  </button>
                ))}
              </div>
            )}
          </div>

          <Button onClick={loadLogs} variant="outline">
            Atualizar
          </Button>
        </PageActions>
      </PageHeader>

      {/* Stats Cards */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <MetricCard
          label="Total de Conversas"
          value={logs.length}
          icon={MessageSquare}
          tone="brand"
        />
        <MetricCard
          label="Tokens Totais"
          value={logs.reduce((sum, log) => sum + (log.tokens_total || 0), 0).toLocaleString()}
          icon={Database}
          tone="info"
        />
        <MetricCard
          label="Tempo Médio"
          value={`${
            logs.length > 0
              ? Math.round(
                  logs.reduce((sum, log) => sum + (log.response_time_ms || 0), 0) / logs.length,
                )
              : 0
          }ms`}
          icon={Clock}
          tone="success"
        />
        <MetricCard
          label="RAG Chunks"
          value={logs.reduce((sum, log) => sum + (log.rag_chunks_count || 0), 0)}
          icon={Database}
          tone="neutral"
        />
      </div>

      {/* Logs Table */}
      <Card className="bg-card border-border">
        <CardContent className="p-0">
          {loading ? (
            <div className="p-8 text-center text-muted-foreground">Carregando...</div>
          ) : logs.length === 0 ? (
            <div className="p-8 text-center text-muted-foreground">Nenhum log encontrado</div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow className="border-border hover:bg-muted/50">
                  <TableHead className="text-muted-foreground">Data/Hora</TableHead>
                  <TableHead className="text-muted-foreground">Empresa</TableHead>
                  <TableHead className="text-muted-foreground">Agente</TableHead>
                  <TableHead className="text-muted-foreground">Usuário</TableHead>

                  <TableHead className="text-muted-foreground">Modelo</TableHead>
                  <TableHead className="text-muted-foreground">Tokens</TableHead>
                  <TableHead className="text-muted-foreground">Tempo</TableHead>
                  <TableHead className="text-muted-foreground w-[150px]">Estratégia RAG</TableHead>
                  <TableHead className="text-muted-foreground"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {paginatedLogs.map((log) => (
                  <React.Fragment key={log.id}>
                    <TableRow
                      className="border-border hover:bg-muted/50 cursor-pointer"
                      onClick={() => toggleRow(log.id)}
                    >
                      <TableCell className="text-sm whitespace-nowrap">
                        {formatDate(log.created_at)}
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center gap-2">
                          <Building2 className="h-4 w-4 text-muted-foreground" />
                          <span className="text-sm whitespace-nowrap">
                            {getCompanyName(log.company_id)}
                          </span>
                        </div>
                      </TableCell>
                      <TableCell>
                        {getAgentName(log.agent_id) ? (
                          <Badge
                            variant="outline"
                            className="flex items-center gap-1 w-fit bg-primary text-primary-foreground border-transparent"
                          >
                            <Bot className="h-3 w-3" />
                            {getAgentName(log.agent_id)}
                          </Badge>
                        ) : (
                          <span className="text-xs text-muted-foreground">-</span>
                        )}
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center gap-1.5">
                          <User className="h-3 w-3 text-muted-foreground" />
                          <span className="text-sm text-muted-foreground">
                            {getUserName(log.user_id)}
                          </span>
                        </div>
                      </TableCell>

                      <TableCell>
                        <Badge
                          className={`${getProviderBadge(log.llm_provider)} text-primary-foreground hover:opacity-90`}
                        >
                          {log.llm_provider}
                        </Badge>
                        <p className="text-xs text-muted-foreground mt-1">{log.llm_model}</p>
                      </TableCell>
                      <TableCell className="text-sm">
                        {log.tokens_total?.toLocaleString() || '-'}
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center gap-1 text-sm whitespace-nowrap">
                          <Clock className="h-3 w-3 text-muted-foreground" />
                          {log.response_time_ms || '-'}ms
                        </div>
                      </TableCell>
                      <TableCell>
                        {/* NOVA VISUALIZAÇÃO DE ESTRATÉGIA */}
                        {getSearchStrategyBadges(log.search_strategy, log.retrieval_score)}
                      </TableCell>
                      <TableCell>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={(e) => {
                            e.stopPropagation();
                            openDetails(log);
                          }}
                        >
                          Detalhes
                        </Button>
                      </TableCell>
                    </TableRow>

                    {/* Expanded Row */}
                    {expandedRow === log.id && (
                      <TableRow className="border-border bg-background">
                        <TableCell colSpan={10} className="p-4">
                          <div className="space-y-4">
                            <div className="grid grid-cols-2 gap-4">
                              <div className="bg-card p-4 rounded border border-border">
                                <h4 className="text-sm font-semibold text-primary mb-2">
                                  Pergunta Completa:
                                </h4>
                                <p className="text-sm text-foreground whitespace-pre-wrap">
                                  {log.user_question}
                                </p>
                              </div>
                              <div className="bg-card p-4 rounded border border-border">
                                <h4 className="text-sm font-semibold text-success mb-2">
                                  Resposta:
                                </h4>
                                <p className="text-sm text-foreground whitespace-pre-wrap">
                                  {log.assistant_response}
                                </p>
                              </div>
                            </div>

                            {log.rag_chunks &&
                              Array.isArray(log.rag_chunks) &&
                              log.rag_chunks.length > 0 && (
                                <div>
                                  <div className="flex items-center gap-2 mb-2">
                                    <Database className="w-4 h-4 text-primary" />
                                    <h4 className="text-sm font-semibold text-muted-foreground">
                                      Chunks usados no RAG ({log.rag_chunks_count}):
                                    </h4>
                                  </div>
                                  <div className="space-y-2">
                                    {log.rag_chunks.map((chunk: any, idx: number) => (
                                      <div
                                        key={idx}
                                        className="bg-card p-3 rounded border border-border"
                                      >
                                        <div className="flex justify-between items-start mb-2">
                                          <span className="text-xs font-medium text-primary">
                                            {chunk.metadata?.document_name || 'Documento sem nome'}
                                          </span>
                                          <div className="flex gap-2">
                                            <Badge
                                              variant="outline"
                                              className={`text-xs ${chunk.score > 0.7 ? 'text-success border-success/20' : 'text-warning border-warning/25'}`}
                                            >
                                              Score: {chunk.score ? chunk.score.toFixed(3) : 'N/A'}
                                            </Badge>
                                            {chunk.used_in_context !== undefined && (
                                              <Badge
                                                className={`text-xs border-transparent ${chunk.used_in_context ? 'bg-primary text-primary-foreground hover:bg-primary/90' : 'bg-secondary text-secondary-foreground'}`}
                                              >
                                                {chunk.used_in_context ? 'Usado' : 'Filtrado'}
                                              </Badge>
                                            )}
                                          </div>
                                        </div>
                                        <p className="text-sm text-foreground  font-mono text-xs whitespace-pre-wrap">
                                          {chunk.content_preview || chunk.content || 'Sem preview'}
                                        </p>
                                      </div>
                                    ))}
                                  </div>
                                </div>
                              )}
                          </div>
                        </TableCell>
                      </TableRow>
                    )}
                  </React.Fragment>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>

        {/* Pagination Controls */}
        {totalPages > 1 && (
          <div className="flex items-center justify-between px-6 py-4 border-t border-border">
            <p className="text-sm text-muted-foreground">
              Mostrando {(currentPage - 1) * itemsPerPage + 1} -{' '}
              {Math.min(currentPage * itemsPerPage, logs.length)} de {logs.length}
            </p>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setCurrentPage((p) => Math.max(1, p - 1))}
                disabled={currentPage === 1}
                className="bg-transparent border-input text-muted-foreground hover:text-foreground"
              >
                Anterior
              </Button>
              <span className="text-sm text-muted-foreground px-2">
                {currentPage} / {totalPages}
              </span>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setCurrentPage((p) => Math.min(totalPages, p + 1))}
                disabled={currentPage === totalPages}
                className="bg-transparent border-input text-muted-foreground hover:text-foreground"
              >
                Próximo
              </Button>
            </div>
          </div>
        )}
      </Card>

      {/* Details Dialog */}
      <Dialog open={detailsOpen} onOpenChange={setDetailsOpen}>
        <DialogContent className="max-w-4xl max-h-[90vh] overflow-y-auto bg-background border-border">
          <DialogHeader>
            <DialogTitle className="text-foreground">Detalhes da Conversa</DialogTitle>
          </DialogHeader>

          {selectedLog && (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <Label className="text-muted-foreground">Empresa</Label>
                  <p className="text-foreground">{getCompanyName(selectedLog.company_id)}</p>
                </div>
                <div>
                  <Label className="text-muted-foreground">Agente</Label>
                  <div className="flex items-center gap-2 mt-1">
                    {getAgentName(selectedLog.agent_id) ? (
                      <Badge
                        variant="outline"
                        className="flex items-center gap-1 bg-primary text-primary-foreground border-transparent"
                      >
                        <Bot className="h-3 w-3" />
                        {getAgentName(selectedLog.agent_id)}
                      </Badge>
                    ) : (
                      <span className="text-muted-foreground">-</span>
                    )}
                  </div>
                </div>
                <div>
                  <Label className="text-muted-foreground">Usuário</Label>
                  <div className="flex items-center gap-2 mt-1">
                    <User className="h-4 w-4 text-muted-foreground" />
                    <p className="text-foreground">{getUserName(selectedLog.user_id)}</p>
                  </div>
                </div>
                <div>
                  <Label className="text-muted-foreground">Session ID</Label>
                  <p className="text-foreground font-mono text-sm">{selectedLog.session_id}</p>
                </div>
                <div>
                  <Label className="text-muted-foreground">Data/Hora</Label>
                  <p className="text-foreground">{formatDate(selectedLog.created_at)}</p>
                </div>
              </div>

              <div className="grid grid-cols-4 gap-4">
                <Card className="bg-muted/50 border-border">
                  <CardContent className="pt-4">
                    <p className="text-xs text-muted-foreground">Tokens Total</p>
                    <p className="text-xl font-bold">{selectedLog.tokens_total || 0}</p>
                  </CardContent>
                </Card>
                <Card className="bg-muted/50 border-border">
                  <CardContent className="pt-4">
                    <p className="text-xs text-muted-foreground">Input</p>
                    <p className="text-xl font-bold">{selectedLog.tokens_input || 0}</p>
                  </CardContent>
                </Card>
                <Card className="bg-muted/50 border-border">
                  <CardContent className="pt-4">
                    <p className="text-xs text-muted-foreground">Output</p>
                    <p className="text-xl font-bold">{selectedLog.tokens_output || 0}</p>
                  </CardContent>
                </Card>
                <Card className="bg-muted/50 border-border">
                  <CardContent className="pt-4">
                    <p className="text-xs text-muted-foreground">RAG Chunks</p>
                    <p className="text-xl font-bold">{selectedLog.rag_chunks_count || 0}</p>
                  </CardContent>
                </Card>
              </div>

              <div>
                <Label className="text-muted-foreground">Modelo e Estratégia</Label>
                <div className="flex items-center gap-2 mt-1">
                  <Badge className={`${getProviderBadge(selectedLog.llm_provider)}`}>
                    {selectedLog.llm_provider}
                  </Badge>
                  <span className="text-foreground">{selectedLog.llm_model}</span>
                  <span className="text-muted-foreground">•</span>
                  {getSearchStrategyBadges(
                    selectedLog.search_strategy,
                    selectedLog.retrieval_score,
                  )}
                </div>
              </div>

              <div>
                <Label className="text-muted-foreground">Pergunta</Label>
                <div className="bg-muted p-3 rounded mt-1">
                  <p className="text-foreground whitespace-pre-wrap">{selectedLog.user_question}</p>
                </div>
              </div>

              <div>
                <Label className="text-muted-foreground">Resposta</Label>
                <div className="bg-muted p-3 rounded mt-1">
                  <p className="text-foreground whitespace-pre-wrap">
                    {selectedLog.assistant_response}
                  </p>
                </div>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </PageShell>
  );
}
