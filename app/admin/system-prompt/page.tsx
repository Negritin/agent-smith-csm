'use client';

import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { AlertTriangle, Loader2, Save } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { Card } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';

interface SystemPromptMeta {
  value: string;
  updated_at: string | null;
  updated_by: string | null;
}

export default function SystemPromptPage() {
  const [value, setValue] = useState('');
  const [original, setOriginal] = useState('');
  const [meta, setMeta] = useState<SystemPromptMeta | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const isEmpty = value.trim().length === 0;
  const isDirty = value !== original;

  useEffect(() => {
    (async () => {
      try {
        const res = await fetch('/api/admin/system-prompt', { credentials: 'include' });
        if (!res.ok) throw new Error('falha ao carregar');
        const data: SystemPromptMeta = await res.json();
        setValue(data.value || '');
        setOriginal(data.value || '');
        setMeta(data);
      } catch {
        toast.error('Não foi possível carregar o system prompt.');
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  async function handleSave() {
    // R1 (client): nunca salvar vazio — o servidor é a autoridade, isto só evita a chamada.
    if (isEmpty) {
      toast.error('O system prompt não pode ficar vazio.');
      return;
    }
    setSaving(true);
    try {
      const res = await fetch('/api/admin/system-prompt', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ value }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data?.detail || 'falha ao salvar');
      }
      setOriginal(data.value || value);
      setValue(data.value || value);
      setMeta(data);
      toast.success('System prompt atualizado. Afeta todos os agentes a partir de agora.');
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Erro ao salvar.');
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24 text-muted-foreground">
        <Loader2 className="mr-2 h-5 w-5 animate-spin" /> Carregando...
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-4xl space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold text-foreground">System Prompt</h1>
        <p className="text-sm text-muted-foreground">
          Prompt de governança global da plataforma. É colado na frente do prompt de cada cliente
          e tem prioridade sobre as instruções deles.
        </p>
      </div>

      {/* Aviso de impacto global */}
      <div className="flex items-start gap-3 rounded-lg border border-amber-500/40 bg-amber-500/10 p-4 text-sm text-amber-700 dark:text-amber-300">
        <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0" />
        <p>
          <strong>Atenção:</strong> este system prompt afeta <strong>todos os agentes do sistema</strong>.
          Altere com cautela.
        </p>
      </div>

      <Card className="p-4">
        <Textarea
          value={value}
          onChange={(e) => setValue(e.target.value)}
          spellCheck={false}
          className="min-h-[440px] font-mono text-sm leading-relaxed"
          placeholder="Conteúdo do system base prompt..."
        />
        {isEmpty && (
          <p className="mt-2 text-sm text-destructive">O system prompt não pode ficar vazio.</p>
        )}
        <div className="mt-4 flex items-center justify-between">
          <span className="text-xs text-muted-foreground">
            {meta?.updated_at
              ? `Última edição: ${new Date(meta.updated_at).toLocaleString('pt-BR')}`
              : 'Sem registro de edição'}
            {meta?.updated_by ? ` · por ${meta.updated_by}` : ''}
          </span>
          <Button
            onClick={() => setConfirmOpen(true)}
            disabled={isEmpty || saving || !isDirty}
          >
            {saving ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <Save className="mr-2 h-4 w-4" />
            )}
            Salvar
          </Button>
        </div>
      </Card>

      {/* R2 — popup de confirmação obrigatório */}
      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title="⚠️ Confirmar alteração do System Prompt"
        description="Este system prompt afeta TODOS os agentes do Sistema. Altere com cautela."
        confirmLabel="Confirmar alteração"
        cancelLabel="Cancelar"
        destructive
        onConfirm={handleSave}
      />
    </div>
  );
}
