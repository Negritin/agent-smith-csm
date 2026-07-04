'use client';

import { useRouter } from 'next/navigation';
import { Button } from '@/components/ui/button';
import { Clock, Mail } from 'lucide-react';
import { PublicStatePage } from '@/components/auth/public-state-page';
import { InlineNotice } from '@/components/ui/feedback-state';

export default function PendingApprovalPage() {
  const router = useRouter();

  return (
    <PublicStatePage
      title="Aguardando aprovação"
      description="Sua conta foi criada com sucesso. Agora aguarde a aprovação do administrador da sua empresa para começar a usar o sistema."
      icon={Clock}
      tone="warning"
      notice={
        <InlineNotice tone="brand" className="flex gap-3">
          <Mail className="mt-0.5 h-5 w-5 flex-shrink-0" />
          <span>
            <span className="block font-semibold">Você receberá um email</span>
            <span className="block text-xs text-muted-foreground">
              Quando sua conta for aprovada, enviaremos um email de confirmação para você.
            </span>
          </span>
        </InlineNotice>
      }
      actions={
        <>
          <Button onClick={() => router.push('/login')} className="w-full">
            Ir para Login
          </Button>
          <Button onClick={() => router.push('/')} variant="outline" className="w-full">
            Voltar para Início
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <h3 className="text-sm font-medium text-foreground">Próximos passos</h3>
        <ol className="list-decimal space-y-2 pl-5 text-sm text-muted-foreground">
          <li>O administrador da empresa receberá uma notificação</li>
          <li>Sua solicitação será revisada e aprovada</li>
          <li>Você receberá um email quando for aprovado</li>
          <li>Faça login e comece a usar o sistema</li>
        </ol>
      </div>
    </PublicStatePage>
  );
}
