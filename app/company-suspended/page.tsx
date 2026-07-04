'use client';

import { useRouter } from 'next/navigation';
import { Button } from '@/components/ui/button';
import { AlertCircle } from 'lucide-react';
import { clearSession } from '@/lib/session';
import { PublicStatePage } from '@/components/auth/public-state-page';
import { InlineNotice } from '@/components/ui/feedback-state';

export default function CompanySuspendedPage() {
  const router = useRouter();

  const handleLogout = async () => {
    try {
      await fetch('/api/auth/logout', {
        method: 'POST',
      });

      clearSession();
      router.push('/login');
    } catch (error) {
      console.error('Erro ao fazer logout:', error);
      clearSession();
      router.push('/login');
    }
  };

  return (
    <PublicStatePage
      title="Acesso suspenso"
      description="A assinatura da sua empresa está suspensa. Entre em contato com o administrador da sua empresa para mais informações."
      icon={AlertCircle}
      tone="danger"
      notice={
        <InlineNotice tone="danger">
          O acesso será restabelecido assim que a situação for regularizada.
        </InlineNotice>
      }
      actions={
        <Button onClick={handleLogout} variant="danger" className="w-full">
          Sair
        </Button>
      }
    />
  );
}
