'use client';

import { BookOpen, Bot, Globe, Loader2, Puzzle, Users, Wrench } from 'lucide-react';

export interface ToolActivity {
  name: string;
  kind: string; // rag | web | mcp | subagent | tool | handoff
}

const KIND_META: Record<string, { label: string; Icon: typeof BookOpen }> = {
  rag: { label: 'Consultando a base de conhecimento', Icon: BookOpen },
  web: { label: 'Pesquisando na web', Icon: Globe },
  mcp: { label: 'Executando integração', Icon: Puzzle },
  subagent: { label: 'Consultando um especialista', Icon: Bot },
  handoff: { label: 'Encaminhando para um atendente', Icon: Users },
  tool: { label: 'Executando uma ação', Icon: Wrench },
};

/**
 * Indicador animado mostrado enquanto o agente executa tools/MCPs/subagents/RAG.
 * Exclusivo da UI do chat web (widget e WhatsApp não usam este stream).
 */
export function ToolActivityIndicator({ activity }: { activity: ToolActivity }) {
  const meta = KIND_META[activity.kind] || KIND_META.tool;
  const { label, Icon } = meta;

  return (
    <div className="flex w-full justify-start mb-4">
      <div className="flex items-center gap-2.5 rounded-2xl rounded-bl-sm border border-border bg-muted/40 px-3 py-2">
        <span className="relative flex h-6 w-6 items-center justify-center">
          <Loader2 className="absolute h-6 w-6 animate-spin text-info/40" />
          <Icon className="h-3.5 w-3.5 text-info" />
        </span>
        <span className="text-sm text-muted-foreground animate-pulse">{label}</span>
        <span className="flex gap-1 ml-0.5">
          <span
            className="w-1.5 h-1.5 rounded-full bg-info/70 animate-bounce"
            style={{ animationDelay: '0ms' }}
          />
          <span
            className="w-1.5 h-1.5 rounded-full bg-info/70 animate-bounce"
            style={{ animationDelay: '150ms' }}
          />
          <span
            className="w-1.5 h-1.5 rounded-full bg-info/70 animate-bounce"
            style={{ animationDelay: '300ms' }}
          />
        </span>
      </div>
    </div>
  );
}
