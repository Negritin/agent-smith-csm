'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Button } from '@/components/ui/button';
import { MetricCard } from '@/components/ui/metric-card';
import { MessageSquare, History, Settings } from 'lucide-react';
import {
  PageActions,
  PageDescription,
  PageHeader,
  PageShell,
  PageTitle,
} from '@/components/ui/page-shell';

export default function DashboardPage() {
  const router = useRouter();
  const [userName, setUserName] = useState('');

  useEffect(() => {
    // Buscar dados do usuário via API (cookie é enviado automaticamente)
    fetch('/api/auth/me')
      .then((res) => res.json())
      .then((data) => {
        if (data.user) {
          setUserName(
            `${data.user.first_name || ''} ${data.user.last_name || ''}`.trim() || 'Usuário',
          );
        }
      })
      .catch((err) => console.error('Error fetching user:', err));
  }, []);

  return (
    <div className="min-h-screen text-foreground">
        <PageShell size="default">
          <PageHeader>
            <div>
              <PageTitle>Bem-vindo, {userName || 'Usuário'}</PageTitle>
              <PageDescription>Sua conta está ativa e pronta para atendimento.</PageDescription>
            </div>
            <PageActions>
              <Button onClick={() => router.push('/dashboard/chat')}>Abrir chat</Button>
            </PageActions>
          </PageHeader>

          <div className="grid gap-4 md:grid-cols-3">
            <MetricCard
              label="Chat"
              value="Novo"
              description="Inicie uma conversa"
              icon={MessageSquare}
              tone="brand"
            />
            <MetricCard
              label="Histórico"
              value="Ativo"
              description="Consulte conversas anteriores"
              icon={History}
              tone="info"
            />
            <MetricCard
              label="Conta"
              value="OK"
              description="Revise seus dados"
              icon={Settings}
              tone="success"
            />
          </div>
        </PageShell>
    </div>
  );
}
