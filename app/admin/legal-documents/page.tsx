'use client';

import { useState, useEffect } from 'react';
import { useAdminRole } from '@/hooks/useAdminRole';
import { useRouter } from 'next/navigation';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Badge } from '@/components/ui/badge';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Checkbox } from '@/components/ui/checkbox';
import { toast } from 'sonner';
import { Plus, Pencil, Trash2, FileText, Eye, Loader2 } from 'lucide-react';
import { EmptyStatePanel, LoadingState } from '@/components/ui/feedback-state';
import {
  PageActions,
  PageDescription,
  PageHeader,
  PageShell,
  PageTitle,
} from '@/components/ui/page-shell';
import { StatusPill } from '@/components/ui/status-pill';

interface LegalDocument {
  id: string;
  type: 'terms_of_use' | 'privacy_policy';
  title: string;
  content: string;
  version: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

const TYPE_LABELS: Record<string, string> = {
  terms_of_use: 'Termos de Uso',
  privacy_policy: 'Política de Privacidade',
};

export default function LegalDocumentsPage() {
  const { role, isLoading: roleLoading } = useAdminRole();
  const router = useRouter();
  const [documents, setDocuments] = useState<LegalDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  // Dialog states
  const [formOpen, setFormOpen] = useState(false);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [editingDoc, setEditingDoc] = useState<LegalDocument | null>(null);
  const [previewDoc, setPreviewDoc] = useState<LegalDocument | null>(null);
  const [deleteDoc, setDeleteDoc] = useState<LegalDocument | null>(null);

  // Form state
  const [formData, setFormData] = useState({
    type: 'terms_of_use' as string,
    title: '',
    content: '',
    version: '',
    is_active: false,
  });

  useEffect(() => {
    if (!roleLoading && role !== 'master') {
      router.push('/admin/team');
    }
  }, [role, roleLoading, router]);

  useEffect(() => {
    if (role === 'master') {
      fetchDocuments();
    }
  }, [role]);

  const fetchDocuments = async () => {
    try {
      setLoading(true);
      const response = await fetch('/api/admin/legal-documents', {
        credentials: 'include',
      });
      if (response.ok) {
        const data = await response.json();
        setDocuments(data.documents || []);
      } else {
        toast.error('Erro ao carregar documentos');
      }
    } catch {
      toast.error('Erro ao conectar com o servidor');
    } finally {
      setLoading(false);
    }
  };

  const openCreateForm = () => {
    setEditingDoc(null);
    setFormData({
      type: 'terms_of_use',
      title: '',
      content: '',
      version: '',
      is_active: false,
    });
    setFormOpen(true);
  };

  const openEditForm = (doc: LegalDocument) => {
    setEditingDoc(doc);
    setFormData({
      type: doc.type,
      title: doc.title,
      content: doc.content,
      version: doc.version,
      is_active: doc.is_active,
    });
    setFormOpen(true);
  };

  const handleSubmit = async () => {
    if (!formData.title || !formData.content || !formData.version) {
      toast.error('Preencha todos os campos obrigatórios');
      return;
    }

    setSaving(true);
    try {
      const url = editingDoc
        ? `/api/admin/legal-documents/${editingDoc.id}`
        : '/api/admin/legal-documents';
      const method = editingDoc ? 'PUT' : 'POST';

      const response = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify(formData),
      });

      if (response.ok) {
        toast.success(
          editingDoc ? 'Documento atualizado com sucesso' : 'Documento criado com sucesso',
        );
        setFormOpen(false);
        fetchDocuments();
      } else {
        const data = await response.json();
        toast.error(data.error || 'Erro ao salvar documento');
      }
    } catch {
      toast.error('Erro ao conectar com o servidor');
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!deleteDoc) return;

    try {
      const response = await fetch(`/api/admin/legal-documents/${deleteDoc.id}`, {
        method: 'DELETE',
        credentials: 'include',
      });

      if (response.ok) {
        toast.success('Documento excluído com sucesso');
        setDeleteConfirmOpen(false);
        setDeleteDoc(null);
        fetchDocuments();
      } else {
        toast.error('Erro ao excluir documento');
      }
    } catch {
      toast.error('Erro ao conectar com o servidor');
    }
  };

  if (roleLoading || role !== 'master') {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <Loader2 className="w-8 h-8 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <PageShell>
      <PageHeader>
        <div>
          <PageTitle className="flex items-center gap-3">
            <FileText className="w-7 h-7 text-primary" />
            Termos e Políticas
          </PageTitle>
          <PageDescription>
            Gerencie os Termos de Uso e Política de Privacidade da plataforma
          </PageDescription>
        </div>
        <PageActions>
          <Button onClick={openCreateForm}>
            <Plus className="w-4 h-4 mr-2" />
            Novo Documento
          </Button>
        </PageActions>
      </PageHeader>

      {/* Documents Table */}
      {loading ? (
        <LoadingState />
      ) : documents.length === 0 ? (
        <EmptyStatePanel
          icon={FileText}
          title="Nenhum documento cadastrado"
          description="Crie o primeiro Termos de Uso ou Política de Privacidade."
        />
      ) : (
        <div className="bg-card rounded-lg border border-border overflow-hidden">
          <table className="w-full">
            <thead>
              <tr className="border-b border-border">
                <th className="text-left text-muted-foreground text-sm font-medium px-6 py-4">
                  Tipo
                </th>
                <th className="text-left text-muted-foreground text-sm font-medium px-6 py-4">
                  Título
                </th>
                <th className="text-left text-muted-foreground text-sm font-medium px-6 py-4">
                  Versão
                </th>
                <th className="text-left text-muted-foreground text-sm font-medium px-6 py-4">
                  Status
                </th>
                <th className="text-left text-muted-foreground text-sm font-medium px-6 py-4">
                  Última Atualização
                </th>
                <th className="text-right text-muted-foreground text-sm font-medium px-6 py-4">
                  Ações
                </th>
              </tr>
            </thead>
            <tbody>
              {documents.map((doc) => (
                <tr
                  key={doc.id}
                  className="border-b border-border last:border-b-0 hover:bg-muted/50 transition-colors"
                >
                  <td className="px-6 py-4">
                    <Badge
                      className={
                        doc.type === 'terms_of_use'
                          ? 'bg-primary text-primary-foreground hover:bg-primary/90 border-transparent'
                          : 'bg-primary text-primary-foreground hover:bg-primary/90 border-transparent'
                      }
                    >
                      {TYPE_LABELS[doc.type]}
                    </Badge>
                  </td>
                  <td className="px-6 py-4 text-foreground font-medium">{doc.title}</td>
                  <td className="px-6 py-4 text-muted-foreground">{doc.version}</td>
                  <td className="px-6 py-4">
                    {doc.is_active ? (
                      <StatusPill tone="success">Ativo</StatusPill>
                    ) : (
                      <StatusPill>Inativo</StatusPill>
                    )}
                  </td>
                  <td className="px-6 py-4 text-muted-foreground text-sm">
                    {new Date(doc.updated_at).toLocaleDateString('pt-BR', {
                      day: '2-digit',
                      month: '2-digit',
                      year: 'numeric',
                      hour: '2-digit',
                      minute: '2-digit',
                    })}
                  </td>
                  <td className="px-6 py-4">
                    <div className="flex items-center justify-end gap-2">
                      <Button
                        variant="ghost"
                        size="icon"
                        className="text-muted-foreground hover:text-foreground hover:bg-muted"
                        onClick={() => {
                          setPreviewDoc(doc);
                          setPreviewOpen(true);
                        }}
                        title="Visualizar"
                      >
                        <Eye className="w-4 h-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="text-muted-foreground hover:text-primary hover:bg-brand-muted"
                        onClick={() => openEditForm(doc)}
                        title="Editar"
                      >
                        <Pencil className="w-4 h-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="text-muted-foreground hover:text-danger hover:bg-danger/10"
                        onClick={() => {
                          setDeleteDoc(doc);
                          setDeleteConfirmOpen(true);
                        }}
                        title="Excluir"
                      >
                        <Trash2 className="w-4 h-4" />
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Create/Edit Dialog */}
      <Dialog open={formOpen} onOpenChange={setFormOpen}>
        <DialogContent className="bg-card border-border text-foreground max-w-2xl max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="text-xl">
              {editingDoc ? 'Editar Documento' : 'Novo Documento'}
            </DialogTitle>
            <DialogDescription className="text-muted-foreground">
              {editingDoc
                ? 'Atualize as informações do documento legal.'
                : 'Crie um novo Termos de Uso ou Política de Privacidade.'}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label className="text-foreground">Tipo</Label>
              <Select
                value={formData.type}
                onValueChange={(value) => setFormData({ ...formData, type: value })}
              >
                <SelectTrigger className="bg-background border-input text-foreground focus:ring-primary">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent className="bg-background border-border">
                  <SelectItem value="terms_of_use" className="text-foreground">
                    Termos de Uso
                  </SelectItem>
                  <SelectItem value="privacy_policy" className="text-foreground">
                    Política de Privacidade
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label className="text-foreground">Título</Label>
                <Input
                  value={formData.title}
                  onChange={(e) => setFormData({ ...formData, title: e.target.value })}
                  placeholder="Ex: Termos de Uso v2.0"
                  className="bg-background border-input text-foreground focus-visible:ring-primary"
                />
              </div>
              <div className="space-y-2">
                <Label className="text-foreground">Versão</Label>
                <Input
                  value={formData.version}
                  onChange={(e) => setFormData({ ...formData, version: e.target.value })}
                  placeholder="Ex: 1.0"
                  className="bg-background border-input text-foreground focus-visible:ring-primary"
                />
              </div>
            </div>

            <div className="space-y-2">
              <Label className="text-foreground">Conteúdo</Label>
              <Textarea
                value={formData.content}
                onChange={(e) => setFormData({ ...formData, content: e.target.value })}
                placeholder="Digite o conteúdo do documento aqui..."
                className="bg-background border-input text-foreground min-h-[300px] resize-y focus-visible:ring-primary"
              />
            </div>

            <div className="flex items-center space-x-2">
              <Checkbox
                id="is_active"
                checked={formData.is_active}
                onCheckedChange={(checked) =>
                  setFormData({ ...formData, is_active: checked as boolean })
                }
                className="border-border data-[state=checked]:bg-primary data-[state=checked]:border-primary"
              />
              <label htmlFor="is_active" className="text-sm text-muted-foreground cursor-pointer">
                Ativar este documento (o documento ativo anterior do mesmo tipo será desativado)
              </label>
            </div>
          </div>

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setFormOpen(false)}
              className="bg-transparent border-input text-muted-foreground hover:text-foreground hover:bg-muted"
            >
              Cancelar
            </Button>
            <Button
              onClick={handleSubmit}
              disabled={saving}
              className="bg-primary hover:bg-primary/90 text-primary-foreground"
            >
              {saving ? (
                <>
                  <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                  Salvando...
                </>
              ) : editingDoc ? (
                'Atualizar'
              ) : (
                'Criar'
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Preview Dialog */}
      <Dialog open={previewOpen} onOpenChange={setPreviewOpen}>
        <DialogContent className="bg-card border-border text-foreground max-w-2xl max-h-[80vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="text-xl flex items-center gap-2">
              <Eye className="w-5 h-5 text-primary" />
              {previewDoc?.title}
            </DialogTitle>
            <DialogDescription className="sr-only">Visualização do documento</DialogDescription>
          </DialogHeader>
          <div className="py-4">
            <div className="flex items-center gap-2 mb-4">
              <Badge
                className={
                  previewDoc?.type === 'terms_of_use'
                    ? 'bg-primary text-primary-foreground hover:bg-primary/90 border-transparent'
                    : 'bg-primary text-primary-foreground hover:bg-primary/90 border-transparent'
                }
              >
                {previewDoc?.type ? TYPE_LABELS[previewDoc.type] : ''}
              </Badge>
              <span className="text-muted-foreground text-sm">v{previewDoc?.version}</span>
              {previewDoc?.is_active && <StatusPill tone="success">Ativo</StatusPill>}
            </div>
            <div className="bg-muted/50 rounded-lg p-6 border border-border whitespace-pre-wrap text-foreground text-sm leading-relaxed max-h-[50vh] overflow-y-auto">
              {previewDoc?.content}
            </div>
          </div>
        </DialogContent>
      </Dialog>

      {/* Delete Confirmation Dialog */}
      <Dialog open={deleteConfirmOpen} onOpenChange={setDeleteConfirmOpen}>
        <DialogContent className="bg-card border-border text-foreground max-w-md">
          <DialogHeader>
            <DialogTitle className="text-xl text-danger">Excluir Documento</DialogTitle>
            <DialogDescription className="sr-only">Confirmar exclusão</DialogDescription>
          </DialogHeader>
          <p className="text-muted-foreground py-4">
            Tem certeza que deseja excluir{' '}
            <span className="font-semibold text-foreground">&quot;{deleteDoc?.title}&quot;</span>?
            <br />
            <span className="text-sm text-muted-foreground">Esta ação não pode ser desfeita.</span>
          </p>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setDeleteConfirmOpen(false)}
              className="bg-transparent border-input text-muted-foreground hover:text-foreground hover:bg-muted"
            >
              Cancelar
            </Button>
            <Button
              onClick={handleDelete}
              className="bg-danger hover:bg-danger/90 text-primary-foreground"
            >
              Excluir
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </PageShell>
  );
}
