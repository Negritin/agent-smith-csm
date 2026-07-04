'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { MessageCircle, Bot } from 'lucide-react';
import { useAdminRole } from '@/hooks/useAdminRole';
import { InlineNotice, LoadingState } from '@/components/ui/feedback-state';
import { PageDescription, PageHeader, PageShell, PageTitle } from '@/components/ui/page-shell';

export default function IntegrationsPage() {
  const { role, isLoading } = useAdminRole();
  const router = useRouter();

  useEffect(() => {
    if (!isLoading && role !== 'company_admin') {
      router.push('/admin');
    }
  }, [role, isLoading, router]);

  if (isLoading) {
    return <LoadingState />;
  }

  return (
    <PageShell>
      <PageHeader>
        <div>
          <PageTitle className="flex items-center gap-3">
            <MessageCircle className="w-8 h-8" />
            Integrações
          </PageTitle>
          <PageDescription>Conecte canais de comunicação ao seu agente</PageDescription>
        </div>
      </PageHeader>

      {/* WhatsApp - Migrado para Agente */}
      <Card className="bg-card border-border mb-6">
        <CardHeader>
          <CardTitle className="text-foreground flex items-center gap-2">
            <MessageCircle className="w-5 h-5 text-success" />
            WhatsApp (Z-API)
          </CardTitle>
        </CardHeader>
        <CardContent>
          <InlineNotice tone="success" className="font-normal">
            <p className="text-success text-sm flex items-center gap-2">
              <Bot className="w-4 h-4" />A configuração do WhatsApp agora é feita{' '}
              <strong>por agente</strong>.
            </p>
            <p className="text-muted-foreground text-sm mt-2">
              Acesse <strong>Agentes, Editar, aba WhatsApp</strong> para configurar a integração de
              cada agente.
            </p>
          </InlineNotice>
        </CardContent>
      </Card>

      {/* Futuras integrações */}
      <div className="grid gap-6 md:grid-cols-2">
        <Card className="bg-card border-border opacity-50">
          <CardHeader>
            <CardTitle className="text-foreground flex items-center gap-2">
              <MessageCircle className="w-5 h-5" />
              Telegram
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-muted-foreground text-sm">Em breve: Integração com Telegram</p>
          </CardContent>
        </Card>

        <Card className="bg-card border-border opacity-50">
          <CardHeader>
            <CardTitle className="text-foreground flex items-center gap-2">
              <MessageCircle className="w-5 h-5" />
              Messenger
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-muted-foreground text-sm">
              Em breve: Integração com Facebook Messenger
            </p>
          </CardContent>
        </Card>
      </div>
    </PageShell>
  );
}
