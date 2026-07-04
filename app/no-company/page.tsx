'use client';

import { useRouter } from 'next/navigation';
import { Button } from '@/components/ui/button';
import { Building2 } from 'lucide-react';
import { clearSession } from '@/lib/session';
import { PublicStatePage } from '@/components/auth/public-state-page';
import { InlineNotice } from '@/components/ui/feedback-state';

export default function NoCompanyPage() {
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
      title="Empresa não vinculada"
      description="Sua conta ainda não está vinculada a nenhuma empresa. Entre em contato com o suporte para vincular sua conta."
      icon={Building2}
      tone="brand"
      notice={
        <InlineNotice tone="brand">
          Entre em contato através do email:{' '}
          {process.env.NEXT_PUBLIC_SUPPORT_EMAIL || 'suporte@exemplo.com'}
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
