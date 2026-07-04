'use client';

import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import { useSidebarCollapse } from '@/hooks/useSidebarCollapse';

/**
 * Props específicas do chat que a `UnifiedSidebar` precisa, mas que nascem na
 * página de chat (`app/dashboard/chat/page.tsx`). Como a sidebar agora é montada
 * UMA vez no `app/dashboard/layout.tsx`, a página de chat registra esses
 * handlers via contexto e a sidebar os lê. Nas demais páginas eles ficam
 * `undefined` (a sidebar usa o fallback de navegação por rota).
 */
interface ChatSidebarProps {
  currentSessionId?: string;
  onSelectConversation?: (sessionId: string) => void;
  onNewConversation?: () => void;
}

interface SidebarContextValue {
  // Estado de colapso (desktop) — persistido em localStorage.
  collapsed: boolean;
  toggle: () => void;
  setCollapsed: (value: boolean) => void;
  mounted: boolean;
  // Props do chat registradas pela página ativa.
  chatProps: ChatSidebarProps;
  setChatProps: (props: ChatSidebarProps) => void;
}

const SidebarContext = createContext<SidebarContextValue | null>(null);

export function SidebarProvider({ children }: { children: React.ReactNode }) {
  const { collapsed, toggle, setCollapsed, mounted } = useSidebarCollapse(
    'smith.sidebar.user.collapsed',
  );
  const [chatProps, setChatProps] = useState<ChatSidebarProps>({});

  const value = useMemo<SidebarContextValue>(
    () => ({ collapsed, toggle, setCollapsed, mounted, chatProps, setChatProps }),
    [collapsed, toggle, setCollapsed, mounted, chatProps],
  );

  return <SidebarContext.Provider value={value}>{children}</SidebarContext.Provider>;
}

export function useSidebarContext(): SidebarContextValue {
  const ctx = useContext(SidebarContext);
  if (!ctx) {
    throw new Error('useSidebarContext deve ser usado dentro de <SidebarProvider>');
  }
  return ctx;
}

/**
 * Helper para a página de chat: registra suas props na sidebar montada no layout
 * e as limpa ao desmontar (evita handlers obsoletos vazarem para outras rotas).
 */
export function useRegisterChatSidebar(props: ChatSidebarProps) {
  const { setChatProps } = useSidebarContext();
  const { currentSessionId, onSelectConversation, onNewConversation } = props;

  useEffect(() => {
    setChatProps({ currentSessionId, onSelectConversation, onNewConversation });
    return () => setChatProps({});
  }, [currentSessionId, onSelectConversation, onNewConversation, setChatProps]);
}
