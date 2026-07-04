'use client';

import { useState, useEffect } from 'react';
import { TermsAcceptanceModal } from '@/components/TermsAcceptanceModal';
import { UnifiedSidebar } from '@/components/UnifiedSidebar';
import { SidebarProvider, useSidebarContext } from '@/components/SidebarContext';
import { useUserId } from '@/hooks/useUserId';

interface ActiveTerms {
  id: string;
  title: string;
  content: string;
  version: string;
}

/**
 * Conteúdo interno do layout: já dentro do <SidebarProvider>, monta a sidebar
 * UMA vez e aplica a margem condicional num único lugar (elimina os 5
 * `lg:ml-64` hardcoded que existiam nas páginas). A página de chat registra
 * suas props (sessão/handlers) via `useRegisterChatSidebar` e elas chegam aqui
 * pelo contexto.
 */
function DashboardShell({ children }: { children: React.ReactNode }) {
  const { userId } = useUserId();
  const { collapsed, mounted, chatProps } = useSidebarContext();

  // Margem do conteúdo: enquanto não hidratar, usa o default expandido
  // (lg:ml-64) para casar com o HTML do servidor e evitar mismatch/salto.
  const contentMargin = mounted && collapsed ? 'lg:ml-16' : 'lg:ml-64';

  return (
    <div className="flex min-h-screen bg-background text-foreground">
      {userId && (
        <UnifiedSidebar
          userId={userId}
          currentSessionId={chatProps.currentSessionId}
          onSelectConversation={chatProps.onSelectConversation}
          onNewConversation={chatProps.onNewConversation}
        />
      )}
      <div className={`flex-1 min-w-0 transition-[margin] duration-200 ${contentMargin}`}>
        {children}
      </div>
    </div>
  );
}

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const [termsOutdated, setTermsOutdated] = useState(false);
  const [activeTerms, setActiveTerms] = useState<ActiveTerms | null>(null);

  useEffect(() => {
    const checkTerms = async () => {
      try {
        const res = await fetch('/api/auth/me', { credentials: 'include' });
        if (res.ok) {
          const data = await res.json();
          if (data.termsOutdated && data.activeTerms) {
            setTermsOutdated(true);
            setActiveTerms(data.activeTerms);
          }
        }
      } catch (error) {
        console.error('Error checking terms:', error);
      }
    };
    checkTerms();
  }, []);

  return (
    <SidebarProvider>
      <DashboardShell>{children}</DashboardShell>
      {termsOutdated && activeTerms && (
        <TermsAcceptanceModal
          activeTerms={activeTerms}
          onAccepted={() => setTermsOutdated(false)}
        />
      )}
    </SidebarProvider>
  );
}
