import * as React from 'react';

import { cn } from '@/lib/utils';

export function ChatFrame({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        'flex h-screen overflow-hidden bg-[#f4f8ff] text-foreground dark:bg-[#090f1c]',
        className,
      )}
      {...props}
    />
  );
}

export function ChatMain({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <main className={cn('relative flex h-full min-w-0 flex-1 flex-col', className)} {...props} />
  );
}

export function ChatTopbar({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <header
      className={cn(
        'z-20 flex h-16 shrink-0 items-center justify-between border-b border-slate-200 bg-white/92 px-4 backdrop-blur supports-[backdrop-filter]:bg-white/86 sm:px-6 dark:border-border dark:bg-[#0b1220]/92 dark:supports-[backdrop-filter]:bg-[#0b1220]/86',
        className,
      )}
      {...props}
    />
  );
}

export const ChatViewport = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      className={cn('min-h-0 flex-1 overflow-y-auto p-4 scroll-smooth sm:p-6', className)}
      {...props}
    />
  ),
);
ChatViewport.displayName = 'ChatViewport';

export function ChatComposerDock({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        'z-10 shrink-0 border-t border-slate-200 bg-[#f4f8ff]/95 px-4 py-4 backdrop-blur sm:px-6 dark:border-border dark:bg-[#090f1c]/95',
        className,
      )}
      {...props}
    />
  );
}

export function ConversationRail({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <aside
      className={cn('flex w-[380px] shrink-0 flex-col border-r border-border bg-card', className)}
      {...props}
    />
  );
}

/**
 * S9 — Terceira coluna (card lateral direito) — SPEC §12.1.
 *
 * `aside` IRMÃO de `ChatMain` (não conteúdo interno do chat). Visível como coluna
 * fixa apenas em `>= 1280px` (`xl`): `w-[360px] shrink-0 border-l bg-card`. Em
 * telas menores fica oculto aqui — o conteúdo é exibido via drawer/overlay
 * acionado por botão no `ChatTopbar` (ver `app/admin/conversations/page.tsx`).
 */
export function ChatDetailsAside({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <aside
      className={cn(
        'hidden w-[360px] min-w-0 shrink-0 overflow-hidden border-l border-border bg-card xl:flex',
        className,
      )}
      {...props}
    />
  );
}
