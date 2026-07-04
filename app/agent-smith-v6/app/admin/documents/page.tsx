'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { FileText, Sparkles } from 'lucide-react';
import { useAdminRole } from '@/hooks/useAdminRole';
import { DocumentManagementModal } from '@/components/admin/DocumentManagementModal';
import Link from 'next/link';
import { InlineNotice, LoadingState } from '@/components/ui/feedback-state';
import {
  PageActions,
  PageDescription,
  PageHeader,
  PageShell,
  PageTitle,
} from '@/components/ui/page-shell';

export default function DocumentsPage() {
  const { role, companyId, isLoading } = useAdminRole();
  const router = useRouter();

  useEffect(() => {
    if (!isLoading && role !== 'company_admin') {
      router.push('/admin');
    }
  }, [role, isLoading, router]);

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
          <PageTitle className="flex items-center gap-3">
            <FileText className="w-8 h-8" />
            Base de Conhecimento
          </PageTitle>
          <PageDescription>
            Faça upload de documentos para treinar seu agente com informações específicas da sua
            empresa
          </PageDescription>
        </div>
      </PageHeader>

      <Card className="bg-card border-border">
        <CardHeader>
          <CardTitle className="text-foreground">Gerenciar Documentos</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-muted-foreground text-sm mb-6">
            Envie PDFs, documentos e outros arquivos que o agente deve conhecer. O sistema
            processará automaticamente e utilizará essas informações para responder aos clientes.
          </p>
          <PageActions className="justify-start">
            <DocumentManagementModal companyId={companyId} companyName="Sua Empresa" />
            <Link href="/admin/knowledge-base/sanitize">
              <button className="inline-flex items-center gap-2 px-4 py-2 bg-primary hover:bg-primary/90 text-primary-foreground text-sm font-medium rounded-lg transition-colors">
                <Sparkles className="w-4 h-4" />
                Sanitizar Documentos
              </button>
            </Link>
          </PageActions>
        </CardContent>
      </Card>
    </PageShell>
  );
}
