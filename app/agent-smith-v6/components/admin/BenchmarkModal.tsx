'use client';

import { useState, Fragment, useCallback } from 'react';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import {
  BarChart3,
  Loader2,
  Trophy,
  Brain,
  Zap,
  FileText,
  AlertCircle,
  CheckCircle2,
  Sparkles,
  Bot,
  Settings2,
  Check,
} from 'lucide-react';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Slider } from '@/components/ui/slider';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Checkbox } from '@/components/ui/checkbox';

import { useEffect } from 'react';
import { useToast } from '@/hooks/use-toast';

interface ModeMetrics {
  avg_score: number;
  true_rate: number;
  raw_scores: number[];
  chunk_quality?: number; // LLM Judge score (1-5)
}

interface Strategy {
  type: string;
  count: number;
  modes: {
    standard: ModeMetrics;
    hyde: ModeMetrics;
    hybrid: ModeMetrics; // NOVO: Modo híbrido (Dense + Sparse BM25)
  };
}

interface BenchmarkResult {
  benchmark_id: string;
  fixed_threshold: number;
  winner: string; // Format: "strategy_mode" (e.g., "semantic_hyde")
  strategies: Strategy[];
  metadata: {
    total_questions: number;
    execution_time_seconds: number;
    winner_true_rate?: number;
  };
}

interface Props {
  companyId: string;
}

interface EligibleDoc {
  id: string;
  file_name: string;
  file_type: string;
  file_size: number;
  chunks_count: number;
  created_at: string;
}

export function BenchmarkModal({ companyId }: Props) {
  const [open, setOpen] = useState(false);
  const [isRunning, setIsRunning] = useState(false);
  const [result, setResult] = useState<BenchmarkResult | null>(null);

  // 🔥 Estados para Job Polling
  const [progress, setProgress] = useState<number>(0);
  const [statusMessage, setStatusMessage] = useState<string>('');

  // 🔥 Novos Estados de Configuração
  const [agents, setAgents] = useState<{ id: string; name: string }[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState<string>('');
  const [threshold, setThreshold] = useState<number>(0.62);
  const [totalQuestions, setTotalQuestions] = useState<number>(10);

  // 🔥 Seleção de documentos
  const [eligibleDocs, setEligibleDocs] = useState<EligibleDoc[]>([]);
  const [selectedDocIds, setSelectedDocIds] = useState<Set<string>>(new Set());
  const [loadingDocs, setLoadingDocs] = useState(false);

  const { toast } = useToast();
  const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL || 'http://localhost:8000';

  useEffect(() => {
    if (open) {
      loadAgents();
    }
  }, [open]);

  const loadAgents = async () => {
    try {
      const response = await fetch(`/api/admin/proxy/agents/company/${companyId}`);
      if (response.ok) {
        const data = await response.json();
        setAgents(data);
        if (data.length === 1) {
          handleAgentChange(data[0].id);
        }
      }
    } catch (error) {
      console.error('Error loading agents:', error);
    }
  };

  const handleAgentChange = useCallback(
    async (agentId: string) => {
      setSelectedAgentId(agentId);
      setSelectedDocIds(new Set());
      setEligibleDocs([]);
      if (!agentId) return;

      setLoadingDocs(true);
      try {
        const response = await fetch(
          `/api/admin/proxy/documents/benchmark/eligible?company_id=${companyId}&agent_id=${agentId}`,
        );
        if (response.ok) {
          const data: EligibleDoc[] = await response.json();
          setEligibleDocs(data);
        }
      } catch (error) {
        console.error('Error loading eligible documents:', error);
      } finally {
        setLoadingDocs(false);
      }
    },
    [companyId, BACKEND_URL],
  );

  const toggleDoc = (docId: string) => {
    setSelectedDocIds((prev) => {
      const next = new Set(prev);
      if (next.has(docId)) {
        next.delete(docId);
      } else if (next.size < 5) {
        next.add(docId);
      }
      return next;
    });
  };

  const toggleSelectAll = () => {
    if (selectedDocIds.size === Math.min(eligibleDocs.length, 5)) {
      setSelectedDocIds(new Set());
    } else {
      setSelectedDocIds(new Set(eligibleDocs.slice(0, 5).map((d) => d.id)));
    }
  };

  const formatFileSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  // 🔥 Helper para mensagens amigáveis de status
  const getStatusLabel = (status: string): string => {
    const labels: Record<string, string> = {
      queued: 'Aguardando início...',
      generating_dataset: 'Gerando perguntas com IA...',
      questions_generated: 'Perguntas geradas!',
      running_recursive: 'Testando: Recursive Chunking...',
      running_semantic: 'Testando: Semantic Chunking...',
      running_page: 'Testando: Page Chunking...',
      running_agentic: 'Testando: Agentic Chunking...',
      completed: 'Finalizado!',
      failed: 'Erro no benchmark',
    };
    return labels[status] || status;
  };

  const runBenchmark = async () => {
    if (!selectedAgentId) {
      toast({
        title: 'Agente Obrigatório',
        description: 'Selecione um agente para executar o benchmark.',
        variant: 'destructive',
      });
      return;
    }
    if (selectedDocIds.size === 0) {
      toast({
        title: 'Documentos Obrigatórios',
        description: 'Selecione ao menos 1 documento para o benchmark.',
        variant: 'destructive',
      });
      return;
    }

    setIsRunning(true);
    setResult(null);
    setProgress(0);
    setStatusMessage('Iniciando...');

    try {
      // 1. Iniciar Job (retorna imediatamente com job_id)
      const startRes = await fetch(`/api/admin/proxy/documents/benchmark/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          company_id: companyId,
          agent_id: selectedAgentId,
          document_ids: Array.from(selectedDocIds),
          threshold: threshold,
          total_questions: totalQuestions,
        }),
      });

      if (!startRes.ok) {
        const error = await startRes.json();
        throw new Error(error.detail || 'Falha ao iniciar benchmark');
      }

      const { job_id } = await startRes.json();

      toast({
        title: 'Benchmark iniciado',
        description: `Job ${job_id.slice(0, 8)}... em execução`,
      });

      // 2. Loop de Polling (a cada 2s)
      const pollInterval = setInterval(async () => {
        try {
          const statusRes = await fetch(
            `/api/admin/proxy/documents/benchmark/status/${job_id}?company_id=${companyId}`,
          );
          if (!statusRes.ok) return;

          const data = await statusRes.json();
          setProgress(data.progress || 0);
          setStatusMessage(getStatusLabel(data.status));

          if (data.status === 'completed') {
            clearInterval(pollInterval);
            setResult(data.result);
            setIsRunning(false);

            // Parse winner
            const [winnerStrategy, winnerMode] = data.result.winner.split('_');
            const modeLabels: Record<string, string> = {
              standard: 'Standard',
              hyde: 'HyDE',
              hybrid: 'Hybrid',
            };
            const winnerLabel = `${getStrategyLabel(winnerStrategy)} (${modeLabels[winnerMode] || winnerMode})`;

            toast({
              title: 'Benchmark concluído',
              description: `Vencedor: ${winnerLabel} - ${(data.result.metadata.winner_true_rate || 0) * 100}% de precisão`,
            });
          } else if (data.status === 'failed') {
            clearInterval(pollInterval);
            setIsRunning(false);
            toast({
              title: 'Erro no Benchmark',
              description: data.result?.error || 'Falha desconhecida',
              variant: 'destructive',
            });
          }
        } catch (e) {
          console.error('Polling error:', e);
        }
      }, 2000);
    } catch (error) {
      console.error('Benchmark error:', error);
      toast({
        title: 'Erro',
        description: error instanceof Error ? error.message : 'Erro ao iniciar benchmark',
        variant: 'destructive',
      });
      setIsRunning(false);
    }
  };

  const getStrategyLabel = (strategy: string) => {
    const labels: Record<string, string> = {
      semantic: 'IA Semantica',
      page: 'Pagina a Pagina',
      recursive: 'Recursive',
      agentic: 'Agent Chunking',
    };
    return labels[strategy] || strategy;
  };

  const getStrategyBadge = (strategy: string, mode: string, isWinner: boolean) => {
    const winnerClass = isWinner ? 'ring-2 ring-warning ring-offset-2 ring-offset-background' : '';

    const strategyBadges: Record<string, { bg: string; text: string; border: string; icon: any }> =
      {
        semantic: {
          bg: 'bg-accent/10',
          text: 'text-accent',
          border: 'border-accent/25',
          icon: Brain,
        },
        page: {
          bg: 'bg-primary/10',
          text: 'text-primary',
          border: 'border-primary/20',
          icon: FileText,
        },
        recursive: {
          bg: 'bg-muted',
          text: 'text-muted-foreground',
          border: 'border-border',
          icon: Zap,
        },
        agentic: {
          bg: 'bg-warning/10',
          text: 'text-warning',
          border: 'border-warning/25',
          icon: Brain,
        },
      };

    const badge = strategyBadges[strategy] || strategyBadges.recursive;
    const Icon = badge.icon;
    const label =
      mode === 'hyde' ? `${getStrategyLabel(strategy)} + HyDE` : getStrategyLabel(strategy);

    return (
      <Badge
        className={`${badge.bg} ${badge.text} ${badge.border} ${winnerClass} flex items-center gap-1`}
      >
        {isWinner && <Trophy className="w-3 h-3 text-warning" />}
        <Icon className="w-3 h-3" />
        {label}
        {mode === 'hyde' && <Sparkles className="w-3 h-3 ml-1 text-info" />}
      </Badge>
    );
  };

  const getScoreColor = (score: number) => {
    if (score >= 0.85) return 'text-success';
    if (score >= 0.7) return 'text-primary';
    if (score >= 0.5) return 'text-warning';
    return 'text-danger';
  };

  const getTrueRateColor = (rate: number) => {
    if (rate >= 0.85) return 'bg-success';
    if (rate >= 0.7) return 'bg-primary';
    if (rate >= 0.5) return 'bg-warning';
    return 'bg-danger';
  };

  const getTrueRateWidth = (rate: number) => {
    return `${Math.round(rate * 100)}%`;
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          variant="outline"
          className="bg-primary hover:bg-primary/90 text-primary-foreground border-transparent"
        >
          <BarChart3 className="mr-2 h-4 w-4" />
          Benchmark Global
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-5xl max-h-[90vh] overflow-y-auto bg-card border-border">
        <DialogHeader>
          <DialogTitle className="text-2xl font-bold text-foreground">
            Benchmark de Estratégias × Modos de Recuperação
          </DialogTitle>
          <p className="text-sm text-muted-foreground mt-2">
            Comparação técnica das estratégias de chunking com Standard vs HyDE vs Hybrid retrieval
          </p>
        </DialogHeader>

        <div className="space-y-4">
          {/* Configurações do Benchmark */}
          {!result && (
            <Card className="bg-background border-border">
              <CardHeader className="pb-3">
                <CardTitle className="text-base flex items-center gap-2 text-foreground">
                  <Settings2 className="w-4 h-4 text-foreground" />
                  Configuração do Teste
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  {/* Agente Selector */}
                  <div className="space-y-2">
                    <Label className="text-xs text-muted-foreground">Agente (Obrigatório)</Label>
                    <Select value={selectedAgentId} onValueChange={handleAgentChange}>
                      <SelectTrigger className="bg-muted border-border text-foreground h-9">
                        <SelectValue placeholder="Selecione um agente..." />
                      </SelectTrigger>
                      <SelectContent className="bg-card border-border">
                        {agents.map((agent) => (
                          <SelectItem key={agent.id} value={agent.id} className="text-foreground">
                            <div className="flex items-center gap-2">
                              <Bot className="w-3 h-3 text-primary" />
                              {agent.name}
                            </div>
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                </div>

                {/* Document Selection */}
                {selectedAgentId && (
                  <div className="space-y-2 col-span-2">
                    <div className="flex items-center justify-between">
                      <Label className="text-xs text-muted-foreground">
                        Documentos para Benchmark (máx. 5)
                      </Label>
                      {eligibleDocs.length > 0 && (
                        <button
                          type="button"
                          onClick={toggleSelectAll}
                          className="text-xs text-primary hover:text-primary transition-colors"
                        >
                          {selectedDocIds.size === Math.min(eligibleDocs.length, 5)
                            ? 'Desmarcar todos'
                            : 'Selecionar todos'}
                        </button>
                      )}
                    </div>
                    <div className="max-h-48 overflow-y-auto rounded-md border border-border bg-muted/30 divide-y divide-border">
                      {loadingDocs ? (
                        <div className="p-4 text-center text-sm text-muted-foreground">
                          <Loader2 className="w-4 h-4 animate-spin inline mr-2" />
                          Carregando documentos...
                        </div>
                      ) : eligibleDocs.length === 0 ? (
                        <div className="p-4 text-center text-sm text-muted-foreground">
                          <AlertCircle className="w-4 h-4 inline mr-2" />
                          Nenhum documento elegível para este agente
                        </div>
                      ) : (
                        eligibleDocs.map((doc) => (
                          <label
                            key={doc.id}
                            className={`flex items-center gap-3 px-3 py-2 cursor-pointer hover:bg-muted/50 transition-colors ${
                              selectedDocIds.has(doc.id) ? 'bg-brand-muted' : ''
                            } ${!selectedDocIds.has(doc.id) && selectedDocIds.size >= 5 ? 'opacity-50 cursor-not-allowed' : ''}`}
                          >
                            <Checkbox
                              checked={selectedDocIds.has(doc.id)}
                              onCheckedChange={() => toggleDoc(doc.id)}
                              disabled={!selectedDocIds.has(doc.id) && selectedDocIds.size >= 5}
                              className="data-[state=checked]:bg-primary data-[state=checked]:border-primary"
                            />
                            <div className="flex-1 min-w-0">
                              <p className="text-sm text-foreground truncate">
                                <FileText className="w-3 h-3 inline mr-1.5 text-muted-foreground" />
                                {doc.file_name}
                              </p>
                              <p className="text-xs text-muted-foreground">
                                {doc.file_type.toUpperCase()} · {formatFileSize(doc.file_size)} ·{' '}
                                {doc.chunks_count} chunks
                              </p>
                            </div>
                          </label>
                        ))
                      )}
                    </div>
                    {selectedDocIds.size > 0 && (
                      <p className="text-xs text-primary">
                        {selectedDocIds.size} documento{selectedDocIds.size > 1 ? 's' : ''}{' '}
                        selecionado{selectedDocIds.size > 1 ? 's' : ''}
                      </p>
                    )}
                  </div>
                )}

                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  {/* Total Questions */}
                  <div className="space-y-2">
                    <Label className="text-xs text-muted-foreground">
                      Total de Perguntas (5-30)
                    </Label>
                    <Input
                      type="number"
                      min={5}
                      max={30}
                      value={totalQuestions}
                      onChange={(e) => setTotalQuestions(parseInt(e.target.value) || 10)}
                      className="bg-muted border-border text-foreground h-9"
                    />
                  </div>

                  {/* Threshold Slider */}
                  <div className="space-y-3">
                    <div className="flex justify-between">
                      <Label className="text-xs text-muted-foreground">
                        Threshold de Similaridade
                      </Label>
                      <span className="text-xs font-mono text-primary">{threshold.toFixed(2)}</span>
                    </div>
                    <Slider
                      value={[threshold]}
                      min={0.0}
                      max={1.0}
                      step={0.01}
                      onValueChange={(vals: number[]) => setThreshold(vals[0])}
                      className="py-1"
                    />
                  </div>
                </div>
              </CardContent>
            </Card>
          )}

          {/* Info Cards (Atualizado para mostrar valores dinâmicos) */}
          <div className="grid grid-cols-3 gap-3">
            <Card className="bg-muted/50 border-border">
              <CardHeader className="pb-2">
                <CardTitle className="text-sm text-muted-foreground">
                  Threshold Configurado
                </CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-2xl font-bold text-foreground">{threshold.toFixed(2)}</p>
              </CardContent>
            </Card>
            <Card className="bg-muted/50 border-border">
              <CardHeader className="pb-2">
                <CardTitle className="text-sm text-muted-foreground">Combinações</CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-2xl font-bold text-foreground">4 × 3 = 12</p>
              </CardContent>
            </Card>
            <Card className="bg-muted/50 border-border">
              <CardHeader className="pb-2">
                <CardTitle className="text-sm text-muted-foreground">Perguntas/Teste</CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-2xl font-bold text-foreground">
                  {result?.metadata.total_questions || totalQuestions}
                </p>
              </CardContent>
            </Card>
          </div>

          {/* Run Button / Progress */}
          {!result && (
            <div className="space-y-4">
              {isRunning ? (
                <div className="space-y-3">
                  <div className="flex justify-between text-sm">
                    <span className="text-muted-foreground">{statusMessage}</span>
                    <span className="text-primary font-mono">{progress}%</span>
                  </div>
                  <div className="h-3 w-full bg-secondary overflow-hidden rounded-full">
                    <div
                      className="h-full bg-primary transition-all duration-500 ease-in-out"
                      style={{ width: `${progress}%` }}
                    />
                  </div>
                  <p className="text-xs text-muted-foreground text-center">
                    O benchmark pode levar alguns minutos. Não feche esta janela.
                  </p>
                </div>
              ) : (
                <Button onClick={runBenchmark} className="w-full" size="lg">
                  <BarChart3 className="mr-2 h-5 w-5" />
                  Iniciar Benchmark
                </Button>
              )}
            </div>
          )}

          {/* Results Matrix */}
          {result && (
            <div className="space-y-4">
              {/* Winner Banner */}
              <Card className="bg-warning/10 border-warning/25">
                <CardHeader>
                  <CardTitle className="flex items-center gap-2 text-warning">
                    <Trophy className="w-5 h-5" />
                    Vencedor: {result.winner.replace('_', ' para ').toUpperCase()}
                  </CardTitle>
                  <CardDescription className="text-muted-foreground">
                    True Rate: {((result.metadata.winner_true_rate || 0) * 100).toFixed(1)}% |
                    Tempo: {result.metadata.execution_time_seconds.toFixed(1)}s
                  </CardDescription>
                </CardHeader>
              </Card>

              {/* Matrix Table */}
              <Card className="bg-muted/50 border-border">
                <CardHeader>
                  <CardTitle className="text-lg">Matriz de Resultados</CardTitle>
                  <CardDescription>
                    Cada estratégia testada com 3 modos: Standard, HyDE e Hybrid (Dense + Sparse
                    BM25)
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <div className="overflow-x-auto">
                    <table className="w-full">
                      <thead>
                        <tr className="border-b border-border">
                          <th className="text-left p-3 text-sm font-semibold text-muted-foreground">
                            Estratégia
                          </th>
                          <th className="text-left p-3 text-sm font-semibold text-muted-foreground">
                            Modo
                          </th>
                          <th className="text-center p-3 text-sm font-semibold text-muted-foreground">
                            Avg Score
                          </th>
                          <th className="text-left p-3 text-sm font-semibold text-muted-foreground">
                            True Rate
                          </th>
                          <th className="text-center p-3 text-sm font-semibold text-accent">
                            LLM Judge
                          </th>
                          <th className="text-center p-3 text-sm font-semibold text-muted-foreground">
                            Status
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {result.strategies.map((strategy) => (
                          <Fragment key={strategy.type}>
                            {/* Standard Mode Row */}
                            <tr className="border-b border-border/50 hover:bg-muted/30">
                              <td className="p-3" rowSpan={3}>
                                {getStrategyBadge(strategy.type, 'standard', false)}
                              </td>
                              <td className="p-3">
                                <span className="text-sm text-muted-foreground">Standard</span>
                              </td>
                              <td className="p-3 text-center">
                                <span
                                  className={`font-mono font-semibold ${getScoreColor(strategy.modes.standard.avg_score)}`}
                                >
                                  {strategy.modes.standard.avg_score.toFixed(3)}
                                </span>
                              </td>
                              <td className="p-3">
                                <div className="flex items-center gap-2">
                                  <div className="flex-1 bg-muted rounded-full h-6 overflow-hidden">
                                    <div
                                      className={`h-full ${getTrueRateColor(strategy.modes.standard.true_rate)} transition-all duration-500`}
                                      style={{
                                        width: getTrueRateWidth(strategy.modes.standard.true_rate),
                                      }}
                                    />
                                  </div>
                                  <span className="text-sm font-semibold text-muted-foreground w-16">
                                    {Math.round(strategy.modes.standard.true_rate * strategy.count)}
                                    /{strategy.count}
                                  </span>
                                </div>
                              </td>
                              <td className="p-3 text-center">
                                <span className="font-mono font-semibold text-accent">
                                  {strategy.modes.standard.chunk_quality?.toFixed(1) || '-'}/5
                                </span>
                              </td>
                              <td className="p-3 text-center">
                                {result.winner === `${strategy.type}_standard` ? (
                                  <Badge className="bg-warning/10 text-warning border-warning/25">
                                    <Trophy className="w-3 h-3 mr-1" />
                                    Vencedor
                                  </Badge>
                                ) : (
                                  <span className="text-muted-foreground">-</span>
                                )}
                              </td>
                            </tr>

                            {/* HyDE Mode Row */}
                            <tr className="border-b border-border hover:bg-info/10">
                              <td className="p-3">
                                <span className="text-sm text-info flex items-center gap-1">
                                  <Sparkles className="w-3 h-3" />
                                  HyDE
                                </span>
                              </td>
                              <td className="p-3 text-center">
                                <span
                                  className={`font-mono font-semibold ${getScoreColor(strategy.modes.hyde.avg_score)}`}
                                >
                                  {strategy.modes.hyde.avg_score.toFixed(3)}
                                </span>
                              </td>
                              <td className="p-3">
                                <div className="flex items-center gap-2">
                                  <div className="flex-1 bg-muted rounded-full h-6 overflow-hidden">
                                    <div
                                      className={`h-full ${getTrueRateColor(strategy.modes.hyde.true_rate)} transition-all duration-500`}
                                      style={{
                                        width: getTrueRateWidth(strategy.modes.hyde.true_rate),
                                      }}
                                    />
                                  </div>
                                  <span className="text-sm font-semibold text-muted-foreground w-16">
                                    {Math.round(strategy.modes.hyde.true_rate * strategy.count)}/
                                    {strategy.count}
                                  </span>
                                </div>
                              </td>
                              <td className="p-3 text-center">
                                <span className="font-mono font-semibold text-accent">
                                  {strategy.modes.hyde.chunk_quality?.toFixed(1) || '-'}/5
                                </span>
                              </td>
                              <td className="p-3 text-center">
                                {result.winner === `${strategy.type}_hyde` ? (
                                  <Badge className="bg-warning/10 text-warning border-warning/25">
                                    <Trophy className="w-3 h-3 mr-1" />
                                    Vencedor
                                  </Badge>
                                ) : (
                                  <span className="text-muted-foreground">-</span>
                                )}
                              </td>
                            </tr>

                            {/* Hybrid Mode Row */}
                            <tr className="border-b border-border hover:bg-brand-muted">
                              <td className="p-3">
                                <span className="text-sm text-primary flex items-center gap-1 font-semibold">
                                  <Zap className="w-3 h-3 text-accent" />
                                  Hybrid
                                </span>
                              </td>
                              <td className="p-3 text-center">
                                <span
                                  className={`font-mono font-semibold ${getScoreColor(strategy.modes.hybrid.avg_score)}`}
                                >
                                  {strategy.modes.hybrid.avg_score.toFixed(3)}
                                </span>
                              </td>
                              <td className="p-3">
                                <div className="flex items-center gap-2">
                                  <div className="flex-1 bg-muted rounded-full h-6 overflow-hidden">
                                    <div
                                      className={`h-full ${getTrueRateColor(strategy.modes.hybrid.true_rate)} transition-all duration-500`}
                                      style={{
                                        width: getTrueRateWidth(strategy.modes.hybrid.true_rate),
                                      }}
                                    />
                                  </div>
                                  <span className="text-sm font-semibold text-muted-foreground w-16">
                                    {Math.round(strategy.modes.hybrid.true_rate * strategy.count)}/
                                    {strategy.count}
                                  </span>
                                </div>
                              </td>
                              <td className="p-3 text-center">
                                <span className="font-mono font-semibold text-accent">
                                  {strategy.modes.hybrid.chunk_quality?.toFixed(1) || '-'}/5
                                </span>
                              </td>
                              <td className="p-3 text-center">
                                {result.winner === `${strategy.type}_hybrid` ? (
                                  <Badge className="bg-warning/10 text-warning border-warning/25">
                                    <Trophy className="w-3 h-3 mr-1" />
                                    Vencedor
                                  </Badge>
                                ) : (
                                  <span className="text-muted-foreground">-</span>
                                )}
                              </td>
                            </tr>
                          </Fragment>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </CardContent>
              </Card>

              {/* Legend */}
              <Card className="bg-muted/30 border-border">
                <CardContent className="pt-6">
                  <div className="grid grid-cols-2 gap-4 text-sm">
                    <div>
                      <p className="text-muted-foreground mb-2 font-semibold">Métricas:</p>
                      <ul className="space-y-1 text-muted-foreground">
                        <li>
                          <strong className="text-foreground">Avg Score:</strong> Similaridade média
                          do Top 1
                        </li>
                        <li>
                          <strong className="text-foreground">True Rate:</strong> % doc correto com
                          score ≥ 0.70
                        </li>
                        <li>
                          <strong className="text-accent">LLM Judge:</strong> Avaliação da qualidade
                          dos chunks (1-5) por Claude Sonnet 4.5
                        </li>
                      </ul>
                    </div>
                    <div>
                      <p className="text-muted-foreground mb-2 font-semibold">Modos:</p>
                      <ul className="space-y-1 text-muted-foreground">
                        <li>
                          <strong className="text-foreground">Standard:</strong> Busca com embedding
                          da pergunta
                        </li>
                        <li>
                          <strong className="text-info">HyDE:</strong> Busca com embedding de
                          documento hipotético
                        </li>
                        <li>
                          <strong className="text-primary">Hybrid:</strong> Fusão Dense + Sparse
                          (BM25)
                        </li>
                      </ul>
                    </div>
                  </div>
                </CardContent>
              </Card>

              {/* Run Again */}
              <Button
                onClick={runBenchmark}
                disabled={isRunning}
                variant="outline"
                className="w-full"
              >
                <BarChart3 className="mr-2 h-4 w-4" />
                Executar Novamente
              </Button>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
