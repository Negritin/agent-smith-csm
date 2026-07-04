'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useAdminRole } from '@/hooks/useAdminRole';
import {
  ArrowLeft,
  Download,
  FileText,
  Loader2,
  RefreshCw,
  Sparkles,
  Trash2,
  Upload,
  X,
  CheckCircle2,
  AlertCircle,
  Clock,
  Eye,
} from 'lucide-react';
import Link from 'next/link';
import { Switch } from '@/components/ui/switch';
import { EmptyStatePanel, InlineNotice, LoadingState } from '@/components/ui/feedback-state';
import { PageDescription, PageHeader, PageShell, PageTitle } from '@/components/ui/page-shell';
import { ProgressMeter } from '@/components/ui/progress-meter';

const ALLOWED_EXTENSIONS = [
  '.pdf',
  '.docx',
  '.doc',
  '.pptx',
  '.xlsx',
  '.html',
  '.png',
  '.jpg',
  '.jpeg',
  '.tiff',
];
const MAX_FILE_SIZE_MB = 50;
const POLL_INTERVAL_MS = 3000;

interface SanitizationJob {
  id: string;
  company_id: string;
  original_filename: string;
  original_file_size: number;
  original_mime_type: string;
  sanitized_file_size: number | null;
  status: string;
  progress: number;
  error_message: string | null;
  pages_count: number | null;
  images_count: number | null;
  tables_count: number | null;
  processing_time_seconds: number | null;
  extract_images: boolean;
  created_at: string;
  updated_at: string;
  expires_at: string;
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(0)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
}

function getStatusInfo(status: string): { icon: React.ReactNode; label: string; color: string } {
  switch (status) {
    case 'pending':
      return {
        icon: <Clock className="w-4 h-4" />,
        label: 'Aguardando',
        color: 'text-muted-foreground',
      };
    case 'uploading':
      return {
        icon: <Upload className="w-4 h-4 animate-pulse" />,
        label: 'Enviando',
        color: 'text-primary',
      };
    case 'parsing':
      return {
        icon: <Loader2 className="w-4 h-4 animate-spin" />,
        label: 'Processando',
        color: 'text-warning',
      };
    case 'cleaning':
      return {
        icon: <Sparkles className="w-4 h-4 animate-pulse" />,
        label: 'Limpando',
        color: 'text-accent',
      };
    case 'completed':
      return {
        icon: <CheckCircle2 className="w-4 h-4" />,
        label: 'Concluído',
        color: 'text-success',
      };
    case 'error':
      return { icon: <AlertCircle className="w-4 h-4" />, label: 'Erro', color: 'text-danger' };
    default:
      return { icon: <Clock className="w-4 h-4" />, label: status, color: 'text-muted-foreground' };
  }
}

export default function SanitizePage() {
  const { role, companyId, isLoading } = useAdminRole();
  const router = useRouter();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [jobs, setJobs] = useState<SanitizationJob[]>([]);
  const [isDragging, setIsDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadErrors, setUploadErrors] = useState<string[]>([]);
  const [extractImages, setExtractImages] = useState(false);

  // Redirect if not company admin
  useEffect(() => {
    if (!isLoading && role !== 'company_admin') {
      router.push('/admin');
    }
  }, [role, isLoading, router]);

  // Fetch jobs
  const fetchJobs = useCallback(async () => {
    if (!companyId) return;
    try {
      const res = await fetch(`/api/sanitization/jobs?company_id=${companyId}`);
      if (res.ok) {
        const data = await res.json();
        setJobs(data.jobs || []);
      }
    } catch (err) {
      console.error('[Sanitize] Error fetching jobs:', err);
    }
  }, [companyId]);

  // Initial fetch
  useEffect(() => {
    if (companyId) fetchJobs();
  }, [companyId, fetchJobs]);

  // Poll for active jobs
  useEffect(() => {
    const hasActiveJobs = jobs.some((j) => j.status !== 'completed' && j.status !== 'error');
    if (!hasActiveJobs) return;

    const interval = setInterval(fetchJobs, POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [jobs, fetchJobs]);

  // Validate file
  const validateFile = (file: File): string | null => {
    const ext = '.' + file.name.split('.').pop()?.toLowerCase();
    if (!ALLOWED_EXTENSIONS.includes(ext)) {
      return `Formato não suportado: ${ext}`;
    }
    if (file.size > MAX_FILE_SIZE_MB * 1024 * 1024) {
      return `Arquivo excede ${MAX_FILE_SIZE_MB}MB`;
    }
    return null;
  };

  // Upload files
  const uploadFiles = async (files: File[]) => {
    if (!companyId || uploading) return;
    setUploading(true);
    setUploadErrors([]);
    const errors: string[] = [];

    for (const file of files) {
      const validationError = validateFile(file);
      if (validationError) {
        errors.push(`${file.name}: ${validationError}`);
        continue;
      }

      try {
        const formData = new FormData();
        formData.append('file', file);
        formData.append('company_id', companyId);
        formData.append('extract_images', String(extractImages));

        const res = await fetch('/api/sanitization/upload', {
          method: 'POST',
          body: formData,
        });

        if (!res.ok) {
          const data = await res.json();
          errors.push(`${file.name}: ${data.detail || 'Erro no upload'}`);
        }
      } catch (err) {
        errors.push(`${file.name}: Erro de conexão`);
      }
    }

    setUploadErrors(errors);
    setUploading(false);
    fetchJobs();
  };

  // Drag & Drop handlers
  const handleDragEnter = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(true);
  };

  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);
  };

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);

    const files = Array.from(e.dataTransfer.files);
    if (files.length > 0) uploadFiles(files);
  };

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    if (files.length > 0) uploadFiles(files);
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  // Download
  const handleDownload = async (job: SanitizationJob) => {
    if (!companyId) return;
    try {
      const res = await fetch(`/api/sanitization/download/${job.id}?company_id=${companyId}`);
      if (res.ok) {
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        const nameWithoutExt = job.original_filename.replace(/\.[^/.]+$/, '');
        a.href = url;
        a.download = `${nameWithoutExt}_sanitized.md`;
        a.click();
        URL.revokeObjectURL(url);
      }
    } catch (err) {
      console.error('[Sanitize] Download error:', err);
    }
  };

  // Delete
  const handleDelete = async (jobId: string) => {
    if (!companyId) return;
    try {
      await fetch(`/api/sanitization/jobs/${jobId}?company_id=${companyId}`, { method: 'DELETE' });
      fetchJobs();
    } catch (err) {
      console.error('[Sanitize] Delete error:', err);
    }
  };

  // Retry (delete + re-upload is manual, but we just delete the failed job)
  const handleRetry = async (job: SanitizationJob) => {
    await handleDelete(job.id);
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
    <PageShell size="default">
      <PageHeader>
        <div>
          <Link
            href="/admin/documents"
            className="inline-flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground transition-colors mb-4"
          >
            <ArrowLeft className="w-4 h-4" />
            Voltar para Base de Conhecimento
          </Link>
          <PageTitle className="flex items-center gap-3">
            <Sparkles className="w-8 h-8 text-foreground" />
            Sanitizar Documentos
          </PageTitle>
          <PageDescription>
            Converta documentos sujos em Markdown limpo e organizado, pronto para a base de
            conhecimento. Nenhuma informação é perdida — apenas a formatação é corrigida.
          </PageDescription>
        </div>
      </PageHeader>

      {/* Upload Area */}
      <div
        onDragEnter={handleDragEnter}
        onDragLeave={handleDragLeave}
        onDragOver={handleDragOver}
        onDrop={handleDrop}
        className={`relative border-2 border-dashed rounded-xl p-10 text-center transition-all duration-200 mb-8 ${
          isDragging
            ? 'border-primary bg-brand-muted scale-[1.01]'
            : 'border-border hover:border-primary/30 bg-card'
        }`}
      >
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept={ALLOWED_EXTENSIONS.join(',')}
          onChange={handleFileSelect}
          className="hidden"
          id="sanitize-file-input"
        />

        <div className="flex flex-col items-center gap-4">
          {uploading ? (
            <Loader2 className="w-12 h-12 text-foreground animate-spin" />
          ) : (
            <div className="w-16 h-16 rounded-2xl bg-brand-muted flex items-center justify-center">
              <Upload className="w-8 h-8 text-foreground" />
            </div>
          )}

          <div>
            <p className="text-foreground font-semibold text-lg mb-1">
              {uploading ? 'Enviando...' : 'Arraste documentos aqui'}
            </p>
            <p className="text-muted-foreground text-sm mb-4">
              ou{' '}
              <button
                onClick={() => fileInputRef.current?.click()}
                disabled={uploading}
                className="text-primary hover:text-primary font-medium underline underline-offset-2 disabled:opacity-50"
              >
                selecione do computador
              </button>
            </p>
            <p className="text-xs text-muted-foreground">
              PDF, DOCX, PPTX, XLSX, HTML, PNG, JPG, TIFF — máx. {MAX_FILE_SIZE_MB}MB por arquivo
            </p>
          </div>
        </div>
      </div>

      {/* Extract Images Toggle */}
      <div className="flex items-center justify-between p-4 bg-card border border-border rounded-lg mb-6">
        <div className="flex items-center gap-3 flex-1">
          <Eye className="w-5 h-5 text-muted-foreground flex-shrink-0" />
          <div>
            <label
              htmlFor="extract-images-page"
              className="text-sm font-medium text-foreground cursor-pointer"
            >
              Analisar imagens e gráficos
            </label>
            <p className="text-xs text-muted-foreground mt-0.5">
              Ative apenas se o documento contiver gráficos, tabelas como imagem ou diagramas
              importantes. Processamento mais lento quando ativado.
            </p>
          </div>
        </div>
        <Switch
          id="extract-images-page"
          checked={extractImages}
          onCheckedChange={setExtractImages}
        />
      </div>

      {/* Upload Errors */}
      {uploadErrors.length > 0 && (
        <div className="mb-6 p-4 bg-danger/10 border border-danger/20 rounded-lg">
          {uploadErrors.map((err, i) => (
            <p key={i} className="text-sm text-danger flex items-center gap-2">
              <AlertCircle className="w-4 h-4 flex-shrink-0" />
              {err}
            </p>
          ))}
          <button
            onClick={() => setUploadErrors([])}
            className="text-xs text-muted-foreground hover:text-foreground mt-2"
          >
            Fechar
          </button>
        </div>
      )}

      {/* Jobs List */}
      {jobs.length > 0 && (
        <div>
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold text-foreground">Documentos ({jobs.length})</h2>
            <button
              onClick={fetchJobs}
              className="text-sm text-muted-foreground hover:text-foreground flex items-center gap-1 transition-colors"
            >
              <RefreshCw className="w-3.5 h-3.5" />
              Atualizar
            </button>
          </div>

          <div className="space-y-3">
            {jobs.map((job) => {
              const statusInfo = getStatusInfo(job.status);
              const isProcessing = !['completed', 'error'].includes(job.status);

              return (
                <div
                  key={job.id}
                  className="bg-card border border-border rounded-lg p-4 transition-all hover:border-border/80"
                >
                  <div className="flex items-start justify-between gap-4">
                    {/* File Info */}
                    <div className="flex items-start gap-3 min-w-0 flex-1">
                      <div className="w-10 h-10 rounded-lg bg-brand-muted flex items-center justify-center flex-shrink-0 mt-0.5">
                        <FileText className="w-5 h-5 text-foreground" />
                      </div>
                      <div className="min-w-0">
                        <p className="font-medium text-foreground truncate">
                          {job.original_filename}
                        </p>
                        <div className="flex items-center gap-3 text-xs text-muted-foreground mt-0.5">
                          <span>{formatFileSize(job.original_file_size)}</span>
                          <span className={`flex items-center gap-1 ${statusInfo.color}`}>
                            {statusInfo.icon}
                            {statusInfo.label}
                          </span>
                          {job.processing_time_seconds && (
                            <span>{formatDuration(job.processing_time_seconds)}</span>
                          )}
                        </div>

                        {/* Metadata for completed jobs */}
                        {job.status === 'completed' && (
                          <div className="flex items-center gap-3 text-xs text-muted-foreground mt-1">
                            {job.pages_count != null && <span>{job.pages_count} páginas</span>}
                            {job.tables_count != null && job.tables_count > 0 && (
                              <span>{job.tables_count} tabelas</span>
                            )}
                            {job.images_count != null && job.images_count > 0 && (
                              <span>{job.images_count} imagens</span>
                            )}
                            {job.sanitized_file_size && (
                              <span>{formatFileSize(job.sanitized_file_size)}</span>
                            )}
                          </div>
                        )}

                        {/* Error message */}
                        {job.status === 'error' && job.error_message && (
                          <p className="text-xs text-danger mt-1 line-clamp-2">
                            {job.error_message}
                          </p>
                        )}
                      </div>
                    </div>

                    {/* Actions */}
                    <div className="flex items-center gap-2 flex-shrink-0">
                      {job.status === 'completed' && (
                        <button
                          onClick={() => handleDownload(job)}
                          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-success hover:bg-success text-primary-foreground text-sm font-medium transition-colors"
                        >
                          <Download className="w-4 h-4" />
                          Download .md
                        </button>
                      )}

                      {job.status === 'error' && (
                        <button
                          onClick={() => handleRetry(job)}
                          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-warning hover:bg-warning text-primary-foreground text-sm font-medium transition-colors"
                        >
                          <RefreshCw className="w-4 h-4" />
                          Remover
                        </button>
                      )}

                      <button
                        onClick={() => handleDelete(job.id)}
                        className="p-1.5 rounded-md text-muted-foreground hover:text-danger hover:bg-danger/10 transition-colors"
                        title="Remover"
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  </div>

                  {/* Progress Bar */}
                  {isProcessing && (
                    <div className="mt-3">
                      <ProgressMeter value={job.progress} caption={`${job.progress}%`} />
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Empty State */}
      {jobs.length === 0 && !uploading && (
        <EmptyStatePanel
          icon={Sparkles}
          title="Nenhum documento sanitizado ainda"
          description="Arraste um arquivo acima para começar."
        />
      )}

      {/* Info Box */}
      <div className="mt-8 p-4 bg-brand-muted border border-primary/20 rounded-lg">
        <p className="text-sm text-foreground">
          <strong>Como funciona:</strong> O Sanitizer analisa o layout do documento com IA, extrai
          texto com OCR quando necessário, preserva tabelas e hierarquia de títulos, e gera Markdown
          limpo. Imagens visuais são convertidas em descrições textuais completas.
        </p>
        <p className="text-xs text-muted-foreground mt-2">
          Após o download, você pode fazer upload do <code>.md</code> na Base de Conhecimento como
          qualquer outro documento. Os arquivos ficam disponíveis por 7 dias.
        </p>
      </div>
    </PageShell>
  );
}
