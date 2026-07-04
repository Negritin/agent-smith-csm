'use client';

import { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { formatDistanceToNow } from 'date-fns';
import { ptBR } from 'date-fns/locale';
import { EmptyStatePanel, LoadingState } from '@/components/ui/feedback-state';
import {
  ObjectList,
  ObjectListActions,
  ObjectListItem,
  ObjectListTitle,
} from '@/components/ui/object-list';
import { PageDescription, PageHeader, PageShell, PageTitle } from '@/components/ui/page-shell';
import { Button } from '@/components/ui/button';
import { MessageSquare } from 'lucide-react';

interface Conversation {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
  message_count?: number;
}

export default function HistoricoPage() {
  const router = useRouter();
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    loadConversations();
  }, []);

  const loadConversations = async () => {
    // Middleware já garante autenticação, não precisa verificar aqui
    try {
      const response = await fetch('/api/conversations?include_counts=true');
      if (!response.ok) throw new Error('Failed to load conversations');

      const data = await response.json();
      setConversations(data.conversations || []);
    } catch (error) {
      console.error('Erro ao carregar conversas:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleOpenConversation = (conversationId: string) => {
    router.push(`/dashboard/chat?conversation=${conversationId}`);
  };

  if (loading) {
    return (
      <div className="min-h-screen bg-background text-foreground flex items-center justify-center">
        <LoadingState label="Carregando histórico..." />
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background text-foreground">
        <PageShell size="default">
          <PageHeader>
            <div>
              <PageTitle>Histórico de Conversas</PageTitle>
              <PageDescription>Retome atendimentos e consultas anteriores.</PageDescription>
            </div>
          </PageHeader>

          {conversations.length === 0 ? (
            <EmptyStatePanel
              icon={MessageSquare}
              title="Você ainda não tem conversas"
              description="Inicie uma conversa para ela aparecer neste histórico."
              action={
                <Button onClick={() => router.push('/dashboard/chat')}>
                  Iniciar primeira conversa
                </Button>
              }
            />
          ) : (
            <ObjectList>
              {conversations.map((conv) => (
                <ObjectListItem
                  key={conv.id}
                  onClick={() => handleOpenConversation(conv.id)}
                  className="cursor-pointer"
                >
                  <div className="flex-1">
                    <ObjectListTitle>{conv.title || 'Conversa sem título'}</ObjectListTitle>
                    <div className="flex items-center gap-3 text-sm text-muted-foreground">
                      <span className="bg-secondary/50 px-2 py-0.5 rounded text-xs">
                        {conv.message_count} msgs
                      </span>
                      <span>•</span>
                      <span>
                        {formatDistanceToNow(new Date(conv.updated_at), {
                          addSuffix: true,
                          locale: ptBR,
                        })}
                      </span>
                    </div>
                  </div>
                  <ObjectListActions>
                    <Button size="sm">Abrir</Button>
                  </ObjectListActions>
                </ObjectListItem>
              ))}
            </ObjectList>
          )}
        </PageShell>
    </div>
  );
}
