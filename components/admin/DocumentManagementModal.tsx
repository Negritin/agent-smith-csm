'use client';

import { useState, useEffect, useCallback } from 'react';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  FileText,
  Upload,
  Trash2,
  Eye,
  AlertCircle,
  CheckCircle,
  Loader2,
  MoreVertical,
  RefreshCw,
  Brain,
  Zap,
  FileStack,
  Bot,
  Database,
  AlertTriangle,
  Table2,
  Sparkles,
} from 'lucide-react';
import { useToast } from '@/hooks/use-toast';
import { BenchmarkModal } from './BenchmarkModal';
import { SanitizationModal } from './SanitizationModal';
import { EmptyStatePanel, InlineNotice, LoadingState } from '@/components/ui/feedback-state';
import { DataField, DataFieldGrid } from '@/components/ui/data-card';
import { ModalSection, ModalSectionTitle } from '@/components/ui/modal-section';
import { StatusPill } from '@/components/ui/status-pill';

interface Document {
  document_id: string;
  file_name: string;
  file_type: string;
  file_size: number;
  status: string;
  chunks_count: number;
  ingestion_strategy?: string;
  ingestion_mode?: string;
  quality_score?: number;
  error_message?: string;
  created_at: string;
  processed_at?: string;
  agent_id?: string | null;
}

interface Props {
  companyId: string;
  companyName: string;
}

export function DocumentManagementModal({ companyId, companyName }: Props) {
  const [open, setOpen] = useState(false);
  const [sanitizationOpen, setSanitizationOpen] = useState(false);
  const [documents, setDocuments] = useState<Document[]>([]);

  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);

  // Strategy selection
  const [selectedStrategy, setSelectedStrategy] = useState<string>('semantic');

  // Ingestion mode: 'semantic' (RAG Híbrido) or 'filesystem' (File System Search)
  const [ingestionMode, setIngestionMode] = useState<string>('semantic');

  // Agent selection
  const [agents, setAgents] = useState<{ id: string; name: string }[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState<string>('');

  // Reprocess dialog state
  const [reprocessDoc, setReprocessDoc] = useState<Document | null>(null);
  const [reprocessStrategy, setReprocessStrategy] = useState<string>('semantic');
  const [reprocessing, setReprocessing] = useState(false);
  const [deleteDocId, setDeleteDocId] = useState<string | null>(null);

  // Filtro da LISTA por agente ('all' = todos). Só afeta a exibição, não o upload.
  const [filterAgentId, setFilterAgentId] = useState<string>('all');

  // 🔥 FIX: Estado para controlar qual dropdown está aberto (evita focus trap deadlock)
  const [openDropdownId, setOpenDropdownId] = useState<string | null>(null);

  // Key para forçar o input de arquivo a limpar seu valor visualmente quando o state selectedFile for limpo
  const [fileInputKey, setFileInputKey] = useState<number>(0);

  const { toast } = useToast();
  const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL || 'http://localhost:8000';

  useEffect(() => {
    if (open) {
      loadDocuments();
      loadAgents();
    }
  }, [open]);

  const loadAgents = async () => {
    try {
      const response = await fetch(`/api/admin/proxy/agents/company/${companyId}`);
      if (response.ok) {
        const data = await response.json();
        setAgents(data);

        // Se só tem 1 agente, seleciona automaticamente
        if (data.length === 1) {
          setSelectedAgentId(data[0].id);
        }
      }
    } catch (error) {
      console.error('Error loading agents:', error);
    }
  };

  const loadDocuments = async () => {
    setLoading(true);
    try {
      const response = await fetch(`/api/admin/proxy/documents/?company_id=${companyId}`);
      if (response.ok) {
        const data = await response.json();
        setDocuments(data);
      }
    } catch (error) {
      console.error('Error loading documents:', error);
      toast({
        title: 'Erro',
        description: 'Falha ao carregar documentos',
        variant: 'destructive',
      });
    } finally {
      setLoading(false);
    }
  };

  const handleFileUpload = async () => {
    // Validação: Agente é obrigatório
    if (!selectedAgentId) {
      toast({
        title: 'Agente obrigatório',
        description: 'Selecione um agente antes de fazer upload do documento.',
        variant: 'destructive',
      });
      return;
    }

    if (!selectedFile) {
      toast({
        title: 'Arquivo obrigatório',
        description: 'Selecione um arquivo para upload.',
        variant: 'destructive',
      });
      return;
    }

    setUploading(true);
    try {
      const formData = new FormData();
      formData.append('file', selectedFile);
      formData.append('company_id', companyId);
      formData.append('strategy', selectedStrategy);
      formData.append('agent_id', selectedAgentId);
      formData.append('ingestion_mode', ingestionMode);

      const response = await fetch(`/api/admin/proxy/documents/upload`, {
        method: 'POST',
        body: formData,
      });

      if (response.ok) {
        const result = await response.json();
        setSelectedFile(null);
        setFileInputKey((k) => k + 1); // Clear HTML input visually
        loadDocuments();

        const agentName = agents.find((a) => a.id === selectedAgentId)?.name || 'Agente';
        toast({
          title: 'Sucesso',
          description: `Documento enviado para ${agentName}. Processando...`,
        });
      } else {
        toast({
          title: 'Erro',
          description: 'Não foi possível enviar o documento.',
          variant: 'destructive',
        });
      }
    } catch (error) {
      console.error('Error uploading document:', error);
      toast({
        title: 'Erro',
        description: 'Erro ao enviar documento',
        variant: 'destructive',
      });
    } finally {
      setUploading(false);
    }
  };

  const handleReprocess = async () => {
    if (!reprocessDoc) return;

    setReprocessing(true);
    try {
      const response = await fetch(`/api/admin/proxy/documents/reprocess`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          document_id: reprocessDoc.document_id,
          company_id: companyId,
          strategy: reprocessStrategy,
        }),
      });

      if (response.ok) {
        setReprocessDoc(null);
        loadDocuments();
        toast({
          title: 'Sucesso',
          description: `Documento sendo reprocessado com ${getStrategyLabel(reprocessStrategy)}`,
        });
      } else {
        toast({
          title: 'Erro',
          description: 'Não foi possível reprocessar o documento.',
          variant: 'destructive',
        });
      }
    } catch (error) {
      console.error('Error reprocessing:', error);
      toast({
        title: 'Erro',
        description: 'Erro ao reprocessar documento',
        variant: 'destructive',
      });
    } finally {
      setReprocessing(false);
    }
  };

  const handleDelete = async (documentId: string) => {
    try {
      const response = await fetch(
        `/api/admin/proxy/documents/${documentId}?company_id=${companyId}`,
        {
          method: 'DELETE',
        },
      );

      if (response.ok) {
        loadDocuments();
        toast({
          title: 'Sucesso',
          description: 'Documento deletado',
        });
      } else {
        toast({
          title: 'Erro',
          description: 'Erro ao deletar documento',
          variant: 'destructive',
        });
      }
    } catch (error) {
      console.error('Error deleting document:', error);
      toast({
        title: 'Erro',
        description: 'Erro ao deletar documento',
        variant: 'destructive',
      });
    }
  };

  const handleOpenDeleteDialog = useCallback((documentId: string) => {
    setOpenDropdownId(null);
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        setDeleteDocId(documentId);
      });
    });
  }, []);

  // 🔥 FIX: Handler para abrir dialog de reprocess SEM focus trap conflict
  const handleOpenReprocessDialog = useCallback((doc: Document) => {
    // 1. Fecha o dropdown PRIMEIRO (síncrono)
    setOpenDropdownId(null);

    // 2. Aguarda o dropdown fechar completamente antes de abrir o dialog
    // Usa requestAnimationFrame para garantir que o DOM atualizou
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        setReprocessDoc(doc);
        setReprocessStrategy(
          doc.file_type === 'csv' ? 'csv' : doc.ingestion_strategy || 'semantic',
        );
      });
    });
  }, []);

  const formatBytes = (bytes: number) => {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + ' ' + sizes[i];
  };

  const getStrategyLabel = (strategy?: string) => {
    switch (strategy) {
      case 'semantic':
        return 'IA Semântica';
      case 'page':
        return 'Página a Página';
      case 'recursive':
        return 'Rápido';
      case 'agentic':
        return 'Agente';
      case 'csv':
        return 'Tabela (CSV)';
      case 'filesystem':
        return 'File System Search';
      default:
        return strategy || 'Desconhecido';
    }
  };

  const getStatusBadge = (status: string) => {
    switch (status) {
      case 'completed':
        return (
          <StatusPill tone="success" className="gap-1">
            <CheckCircle className="w-3 h-3 mr-1" />
            Completo
          </StatusPill>
        );
      case 'processing':
      case 'pending':
        return (
          <StatusPill tone="brand" className="gap-1">
            <Loader2 className="w-3 h-3 mr-1 animate-spin" />
            Processando...
          </StatusPill>
        );
      case 'failed':
        return (
          <StatusPill tone="danger" className="gap-1">
            <AlertCircle className="w-3 h-3 mr-1" />
            Falhou
          </StatusPill>
        );
      default:
        return <StatusPill>{status}</StatusPill>;
    }
  };

  const getStrategyBadge = (strategy?: string) => {
    switch (strategy) {
      case 'semantic':
        return (
          <Badge className="bg-primary text-primary-foreground border-transparent">
            <Brain className="w-3 h-3 mr-1" />
            IA Semântica
          </Badge>
        );
      case 'page':
        return (
          <Badge className="bg-primary text-primary-foreground border-transparent">
            <FileStack className="w-3 h-3 mr-1" />
            Página
          </Badge>
        );
      case 'recursive':
        return (
          <Badge className="bg-primary text-primary-foreground border-transparent">
            <Zap className="w-3 h-3 mr-1" />
            Rápido
          </Badge>
        );
      case 'agentic':
        return (
          <Badge className="bg-primary text-primary-foreground border-transparent">
            <Brain className="w-3 h-3 mr-1" />
            Agente
          </Badge>
        );
      case 'csv':
        return (
          <Badge className="bg-success text-primary-foreground border-transparent">
            <Table2 className="w-3 h-3 mr-1" />
            Tabela
          </Badge>
        );
      default:
        return null;
    }
  };

  const getIngestionModeBadge = (mode?: string) => {
    if (mode === 'filesystem') {
      return (
        <Badge className="bg-accent text-accent-foreground border-transparent">
          <FileText className="w-3 h-3 mr-1" />
          File System
        </Badge>
      );
    }
    return null;
  };

  const getAgentName = (agentId?: string | null) => {
    if (!agentId) return 'Sem agente';
    const agent = agents.find((a) => a.id === agentId);
    return agent ? agent.name : 'Desconhecido';
  };

  // Verifica se pode fazer upload (arquivo + agente selecionados)
  const canUpload = selectedFile && selectedAgentId;

  // ─── Filtro por agente (somente exibição) ─────────────────────────────────
  // Agrupa os docs por agente (agent_id null/legado vira '__none__') só para
  // montar as opções do filtro com contagem. As opções refletem apenas agentes
  // que de fato têm documentos — assim a lista não fica poluída.
  const FILTER_NONE = '__none__';
  const docCountByAgent = documents.reduce<Record<string, number>>((acc, d) => {
    const key = d.agent_id || FILTER_NONE;
    acc[key] = (acc[key] || 0) + 1;
    return acc;
  }, {});
  const agentFilterOptions = Object.keys(docCountByAgent)
    .map((id) => ({ id, name: getAgentName(id === FILTER_NONE ? null : id), count: docCountByAgent[id] }))
    .sort((a, b) => a.name.localeCompare(b.name, 'pt-BR'));
  const filteredDocuments =
    filterAgentId === 'all'
      ? documents
      : documents.filter((d) => (d.agent_id || FILTER_NONE) === filterAgentId);

  return (
    <>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogTrigger asChild>
          <Button className="bg-primary hover:bg-primary/90 text-primary-foreground">
            <Database className="w-4 h-4 mr-2" />
            Base de Conhecimento
          </Button>
        </DialogTrigger>
        <DialogContent className="bg-card border-border text-foreground max-w-6xl max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="text-2xl font-bold text-foreground">
              Base de Conhecimento RAG - {companyName}
            </DialogTitle>
          </DialogHeader>

          {/* Upload Card */}
          <ModalSection className="mb-6">
            <div className="space-y-4">
              {/* Alerta se não tem agentes */}
              {agents.length === 0 && (
                <InlineNotice tone="warning" className="flex items-center gap-2 font-normal">
                  <AlertTriangle className="w-5 h-5 text-warning" />
                  <p className="text-sm text-warning">
                    Nenhum agente cadastrado. Crie um agente antes de fazer upload de documentos.
                  </p>
                </InlineNotice>
              )}

              <div className="grid grid-cols-1 md:grid-cols-12 gap-4">
                {/* File Input */}
                <div className="md:col-span-3">
                  <label className="text-sm font-medium text-foreground mb-2 block">Arquivo</label>
                  <input
                    key={fileInputKey}
                    type="file"
                    accept={ingestionMode === 'filesystem' ? '.md' : '.pdf,.docx,.txt,.md,.csv'}
                    onChange={(e) => {
                      const file = e.target.files?.[0] || null;
                      setSelectedFile(file);
                      if (file && file.name.toLowerCase().endsWith('.csv')) {
                        setSelectedStrategy('csv');
                      } else if (selectedStrategy === 'csv') {
                        setSelectedStrategy('semantic');
                      }
                    }}
                    className="w-full text-sm text-muted-foreground file:mr-4 file:py-2 file:px-4 file:rounded file:border-0 file:text-sm file:font-semibold file:bg-primary file:text-primary-foreground hover:file:bg-primary/90"
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    {ingestionMode === 'filesystem'
                      ? 'Somente Markdown (.md) — Max 10MB'
                      : 'PDF, DOCX, TXT, MD, CSV (Max 10MB)'}
                  </p>
                </div>

                {/* Agent Selector - OBRIGATÓRIO */}
                <div className="md:col-span-4">
                  <label className="text-sm font-medium text-foreground mb-2 block">
                    Vincular ao Agente <span className="text-danger">*</span>
                  </label>
                  <Select
                    value={selectedAgentId}
                    onValueChange={setSelectedAgentId}
                    disabled={agents.length === 0}
                  >
                    <SelectTrigger
                      className={`bg-background border-input text-foreground ${
                        !selectedAgentId && selectedFile ? 'border-danger/30' : ''
                      }`}
                    >
                      <SelectValue placeholder="Selecione um agente..." />
                    </SelectTrigger>
                    <SelectContent className="bg-popover border-border text-popover-foreground">
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
                  {!selectedAgentId && selectedFile && (
                    <p className="text-xs text-danger mt-1">Selecione um agente para continuar</p>
                  )}
                </div>

                {/* Ingestion Mode Selector */}
                <div className="md:col-span-2">
                  <label className="text-sm font-medium text-foreground mb-2 block">Modo</label>
                  <Select
                    value={ingestionMode}
                    onValueChange={(val) => {
                      setIngestionMode(val);
                      if (val === 'filesystem') {
                        setSelectedStrategy('semantic');
                        // Apenas reseta o arquivo se for inválido para o File System Search (deve ser .md)
                        if (selectedFile && !selectedFile.name.toLowerCase().endsWith('.md')) {
                          setSelectedFile(null);
                          setFileInputKey((k) => k + 1);
                        }
                      }
                    }}
                  >
                    <SelectTrigger className="bg-background border-input text-foreground">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent className="bg-popover border-border text-popover-foreground">
                      <SelectItem value="semantic">RAG Semântico</SelectItem>
                      <SelectItem value="filesystem">File System Search</SelectItem>
                    </SelectContent>
                  </Select>
                </div>

                {/* Strategy Selector — HIDDEN when filesystem mode */}
                {ingestionMode !== 'filesystem' && (
                  <div className="md:col-span-2">
                    <label className="text-sm font-medium text-foreground mb-2 block">
                      Chunking
                    </label>
                    {(() => {
                      const isCsv = selectedFile?.name?.toLowerCase().endsWith('.csv');
                      return (
                        <Select
                          value={selectedStrategy}
                          onValueChange={setSelectedStrategy}
                          disabled={isCsv}
                        >
                          <SelectTrigger className="bg-background border-input text-foreground">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent className="bg-popover border-border text-popover-foreground">
                            <SelectItem value="agentic" className="text-warning" disabled={isCsv}>
                              Agent Chunking
                            </SelectItem>
                            <SelectItem value="semantic" className="text-accent" disabled={isCsv}>
                              IA Semântica
                            </SelectItem>
                            <SelectItem value="page" className="text-primary" disabled={isCsv}>
                              Página a Página
                            </SelectItem>
                            <SelectItem
                              value="recursive"
                              className="text-muted-foreground"
                              disabled={isCsv}
                            >
                              Rápido
                            </SelectItem>
                            <SelectItem value="csv" disabled={!isCsv}>
                              Tabela (CSV)
                            </SelectItem>
                          </SelectContent>
                        </Select>
                      );
                    })()}
                  </div>
                )}
              </div>

              {/* Warning for filesystem mode */}
              {ingestionMode === 'filesystem' && (
                <InlineNotice tone="info" className="flex items-center gap-2 font-normal">
                  <AlertTriangle className="w-5 h-5 text-accent flex-shrink-0" />
                  <p className="text-sm text-accent">
                    <strong>File System Search:</strong> Limite de 1 documento por sub-agente neste
                    modo. O agente navegará o documento inteiro ao invés de usar chunks.
                  </p>
                </InlineNotice>
              )}

              <div className="flex items-center justify-end pt-2">
                <Button
                  onClick={handleFileUpload}
                  disabled={!canUpload || uploading}
                  className="bg-primary hover:bg-primary/90 text-primary-foreground disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {uploading ? (
                    <>
                      <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                      Processando...
                    </>
                  ) : (
                    <>
                      <Upload className="w-4 h-4 mr-2" />
                      Fazer Upload
                    </>
                  )}
                </Button>
              </div>
            </div>
          </ModalSection>

          {/* Benchmark + Sanitization - Buttons only, modals rendered outside parent Dialog */}
          <div className="flex items-center gap-3 mb-6">
            <BenchmarkModal companyId={companyId} />
            <Button
              variant="outline"
              className="bg-primary hover:bg-primary/90 text-primary-foreground border-primary"
              onClick={() => {
                setOpen(false);
                setTimeout(() => setSanitizationOpen(true), 150);
              }}
            >
              <Sparkles className="w-4 h-4 mr-2" />
              Sanitizar Documentos
            </Button>
          </div>

          {/* Lista de Documentos */}
          <div className="space-y-3">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
              <ModalSectionTitle className="text-lg">
                Documentos (
                {filterAgentId === 'all'
                  ? documents.length
                  : `${filteredDocuments.length} de ${documents.length}`}
                )
              </ModalSectionTitle>
              {documents.length > 0 && (
                <div className="flex items-center gap-2">
                  <Bot className="h-4 w-4 text-muted-foreground" />
                  <Select value={filterAgentId} onValueChange={setFilterAgentId}>
                    <SelectTrigger className="w-[230px] bg-background border-border text-foreground">
                      <SelectValue placeholder="Filtrar por agente" />
                    </SelectTrigger>
                    <SelectContent className="bg-card border-border">
                      <SelectItem value="all" className="text-foreground">
                        Todos os agentes ({documents.length})
                      </SelectItem>
                      {agentFilterOptions.map((opt) => (
                        <SelectItem key={opt.id} value={opt.id} className="text-foreground">
                          {opt.name} ({opt.count})
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              )}
            </div>
            {loading ? (
              <LoadingState label="Carregando documentos..." className="min-h-[160px]" />
            ) : documents.length === 0 ? (
              <EmptyStatePanel
                icon={FileText}
                title="Nenhum documento encontrado"
                description="Os documentos enviados aparecerão aqui."
              />
            ) : filteredDocuments.length === 0 ? (
              <EmptyStatePanel
                icon={FileText}
                title="Nenhum documento para este agente"
                description={`"${getAgentName(filterAgentId === FILTER_NONE ? null : filterAgentId)}" não tem documentos. Troque o filtro para ver os demais.`}
              />
            ) : (
              filteredDocuments.map((doc) => (
                <Card
                  key={doc.document_id}
                  className="bg-card border-border hover:bg-muted/50 transition-colors"
                >
                  <CardContent className="p-4">
                    <div className="flex items-start justify-between">
                      <div className="flex-1">
                        <div className="flex items-center gap-2 mb-2 flex-wrap">
                          <FileText className="w-4 h-4 text-primary" />
                          <h4 className="text-foreground font-medium">{doc.file_name}</h4>
                          {getStatusBadge(doc.status)}
                          {doc.ingestion_mode === 'filesystem'
                            ? getIngestionModeBadge(doc.ingestion_mode)
                            : getStrategyBadge(doc.ingestion_strategy)}
                          {/* Badge do Agente */}
                          <Badge
                            variant="outline"
                            className={`border-transparent bg-primary text-primary-foreground`}
                          >
                            <Bot className="w-3 h-3 mr-1" />
                            {getAgentName(doc.agent_id)}
                          </Badge>
                        </div>
                        {/* Info grid */}
                        <DataFieldGrid className="grid-cols-2 text-sm md:grid-cols-4">
                          <DataField label="Tipo" value={doc.file_type.toUpperCase()} />
                          <DataField label="Tamanho" value={formatBytes(doc.file_size)} />
                          <DataField label="Chunks" value={doc.chunks_count} />
                          <DataField
                            label="Upload"
                            value={new Date(doc.created_at).toLocaleDateString('pt-BR')}
                          />
                        </DataFieldGrid>
                        {doc.error_message && (
                          <p className="text-danger text-xs mt-2">
                            Erro: falha ao processar o documento.
                          </p>
                        )}
                      </div>

                      {/* 🔥 FIX: Dropdown com controle de estado explícito */}
                      <DropdownMenu
                        open={openDropdownId === doc.document_id}
                        onOpenChange={(isOpen) => {
                          setOpenDropdownId(isOpen ? doc.document_id : null);
                        }}
                      >
                        <DropdownMenuTrigger asChild>
                          <Button
                            size="sm"
                            variant="outline"
                            className="bg-transparent border-border text-muted-foreground hover:text-foreground ml-4"
                          >
                            <MoreVertical className="w-4 h-4" />
                          </Button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent className="bg-card border-border" align="end">
                          <DropdownMenuItem
                            className="text-muted-foreground hover:bg-muted cursor-pointer"
                            onClick={() => {
                              setOpenDropdownId(null); // Fecha dropdown
                              window.open(
                                `/api/admin/proxy/documents/chunks/${companyId}?document_id=${doc.document_id}`,
                                '_blank',
                              );
                            }}
                          >
                            <Eye className="w-4 h-4 mr-2" /> Ver Chunks
                          </DropdownMenuItem>
                          <DropdownMenuItem
                            className="text-primary hover:bg-muted cursor-pointer"
                            onClick={() => handleOpenReprocessDialog(doc)}
                          >
                            <RefreshCw className="w-4 h-4 mr-2" /> Trocar Inteligência
                          </DropdownMenuItem>
                          <DropdownMenuSeparator className="bg-border" />
                          <DropdownMenuItem
                            className="text-danger hover:bg-muted cursor-pointer"
                            onClick={() => {
                              handleOpenDeleteDialog(doc.document_id);
                            }}
                          >
                            <Trash2 className="w-4 h-4 mr-2" /> Deletar
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </div>
                  </CardContent>
                </Card>
              ))
            )}
          </div>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={!!deleteDocId}
        onOpenChange={(isOpen) => {
          if (!isOpen) setDeleteDocId(null);
        }}
        title="Deletar documento?"
        description="Esta ação remove o documento da base de conhecimento."
        confirmLabel="Deletar"
        destructive
        onConfirm={() => {
          const documentId = deleteDocId;
          setDeleteDocId(null);
          if (documentId) {
            void handleDelete(documentId);
          }
        }}
      />

      {/* Reprocess Dialog - 🔥 FIX: Adicionado modal={true} explícito */}
      <Dialog
        open={!!reprocessDoc}
        onOpenChange={(isOpen) => {
          if (!isOpen) {
            setReprocessDoc(null);
          }
        }}
        modal={true}
      >
        <DialogContent className="bg-card border-border text-foreground">
          <DialogHeader>
            <DialogTitle>Trocar Estratégia de Chunking</DialogTitle>
          </DialogHeader>

          <div className="space-y-4 py-4">
            <div className="p-3 bg-card border border-border rounded">
              <p className="text-sm text-muted-foreground">Arquivo:</p>
              <p className="font-medium text-foreground">{reprocessDoc?.file_name}</p>
            </div>

            {/* Mostra o agente do documento */}
            <div className="p-3 bg-card border border-border rounded">
              <p className="text-sm text-muted-foreground">Agente:</p>
              <p className="font-medium text-primary flex items-center gap-2">
                <Bot className="w-4 h-4" />
                {getAgentName(reprocessDoc?.agent_id)}
              </p>
            </div>

            <div className="space-y-2">
              <label className="text-sm font-medium text-foreground">Nova Estratégia</label>
              {(() => {
                const isCsv = reprocessDoc?.file_type === 'csv';
                return (
                  <Select
                    value={reprocessStrategy}
                    onValueChange={setReprocessStrategy}
                    disabled={isCsv}
                  >
                    <SelectTrigger className="bg-background border-input text-foreground">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent className="bg-popover border-border text-popover-foreground">
                      <SelectItem value="agentic" disabled={isCsv}>
                        Agent Chunking
                      </SelectItem>
                      <SelectItem value="semantic" disabled={isCsv}>
                        IA Semântica
                      </SelectItem>
                      <SelectItem value="page" disabled={isCsv}>
                        Página a Página
                      </SelectItem>
                      <SelectItem value="recursive" disabled={isCsv}>
                        Recursive
                      </SelectItem>
                      <SelectItem value="csv" disabled={!isCsv}>
                        Tabela (CSV)
                      </SelectItem>
                    </SelectContent>
                  </Select>
                );
              })()}
              <p className="text-xs text-muted-foreground">
                O documento será re-indexado do zero com a nova estratégia
              </p>
            </div>
          </div>

          <div className="flex justify-end gap-3">
            <Button
              variant="outline"
              onClick={() => setReprocessDoc(null)}
              className="bg-transparent border-input text-muted-foreground hover:text-foreground"
            >
              Cancelar
            </Button>
            <Button
              onClick={handleReprocess}
              disabled={reprocessing}
              className="bg-primary hover:bg-primary/90 text-primary-foreground"
            >
              {reprocessing ? (
                <>
                  <Loader2 className="w-4 h-4 mr-2 animate-spin" /> Processando...
                </>
              ) : (
                'Confirmar Troca'
              )}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {/* Sanitization Modal - rendered outside parent Dialog to avoid clipping */}
      <SanitizationModal
        companyId={companyId}
        externalOpen={sanitizationOpen}
        onExternalClose={() => setSanitizationOpen(false)}
      />
    </>
  );
}
