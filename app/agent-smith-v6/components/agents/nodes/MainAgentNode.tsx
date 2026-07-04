'use client';

import { Handle, Position, type NodeProps, type Node } from '@xyflow/react';
import { Bot, Edit, Archive, CheckCircle, XCircle, Users } from 'lucide-react';

type MainAgentNodeData = {
  agent: {
    id: string;
    name: string;
    slug: string;
    is_active: boolean;
    llm_provider?: string;
    llm_model?: string;
    allow_web_search: boolean;
    allow_vision: boolean;
    has_api_key?: boolean;
    has_whatsapp: boolean;
    is_subagent?: boolean;
  };
  subCount: number;
  onEdit: (id: string) => void;
  onArchive: (id: string) => void;
};

export function MainAgentNode({ data }: NodeProps<Node<MainAgentNodeData>>) {
  const { agent, subCount, onEdit, onArchive } = data;

  return (
    <div className="main-agent-node group" style={{ width: 320 }}>
      <div
        className="
          dark relative rounded-xl border border-border bg-card shadow-[var(--shadow-border)]
          transition-all duration-300
          group-hover:border-primary/35 group-hover:shadow-[var(--shadow-raised)]
        "
      >
        {/* Header */}
        <div className="px-4 pt-4 pb-3">
          <div className="flex items-start justify-between">
            <div className="flex items-center gap-3">
              <div className="p-2 bg-brand-muted rounded-lg border border-primary/20">
                <Bot className="w-5 h-5 text-primary" />
              </div>
              <div>
                <h3 className="text-[15px] font-semibold text-foreground leading-tight">
                  {agent.name}
                </h3>
                <p className="text-xs text-muted-foreground mt-0.5">/{agent.slug}</p>
              </div>
            </div>
            <div className="flex items-center">
              {agent.is_active ? (
                <span className="flex items-center gap-1 text-[11px] font-medium text-success bg-success/10 border border-success/20 px-2 py-0.5 rounded-full">
                  <CheckCircle className="w-3 h-3" />
                  Ativo
                </span>
              ) : (
                <span className="flex items-center gap-1 text-[11px] font-medium text-muted-foreground bg-muted border border-border px-2 py-0.5 rounded-full">
                  <XCircle className="w-3 h-3" />
                  Inativo
                </span>
              )}
            </div>
          </div>
        </div>

        {/* Model Info */}
        <div className="px-4 pb-3 space-y-1.5">
          <div className="flex items-center justify-between text-[13px]">
            <span className="text-muted-foreground">Provider</span>
            <span className="text-muted-foreground font-medium">{agent.llm_provider || '—'}</span>
          </div>
          <div className="flex items-center justify-between text-[13px]">
            <span className="text-muted-foreground">Modelo</span>
            <span className="text-muted-foreground font-medium truncate max-w-[160px]">
              {agent.llm_model || '—'}
            </span>
          </div>
        </div>

        {/* Capabilities */}
        <div className="px-4 pb-3 flex flex-wrap gap-1.5">
          {agent.allow_web_search && (
            <span className="inline-flex items-center rounded-md border text-[10px] px-1 py-0 border-primary/20 text-primary">
              Web
            </span>
          )}
          {agent.allow_vision && (
            <span className="inline-flex items-center rounded-md border text-[10px] px-1 py-0 border-info/35 text-info">
              Vision
            </span>
          )}
          {agent.has_whatsapp && (
            <span className="inline-flex items-center rounded-md border text-[10px] px-1 py-0 border-success/30 text-success">
              WhatsApp
            </span>
          )}
        </div>

        {/* Actions */}
        <div className="px-4 pb-3 flex gap-2 border-t border-border pt-3">
          <button
            onClick={() => onEdit(agent.id)}
            className="
              flex-1 flex items-center justify-center gap-1.5 text-[12px] font-medium
              py-1.5 rounded-lg
              bg-brand-muted border border-primary/25 text-white
              hover:bg-primary/10 hover:border-primary/40
              transition-all duration-200 cursor-pointer
            "
          >
            <Edit className="w-3 h-3" />
            Editar
          </button>
          <button
            onClick={() => onArchive(agent.id)}
            className="
              flex items-center justify-center gap-1.5 text-[12px] font-medium
              py-1.5 px-3 rounded-lg
              bg-danger/10 border border-danger/25 text-danger
              hover:bg-danger/10 hover:border-danger/40
              transition-all duration-200 cursor-pointer
            "
          >
            <Archive className="w-3 h-3" />
          </button>
        </div>

        {/* Sub-agent count badge */}
        {subCount > 0 && (
          <div className="px-4 pb-3 flex justify-center">
            <span className="flex items-center gap-1.5 text-[11px] font-medium text-white bg-brand-muted border border-primary/20 px-3 py-1 rounded-full">
              <Users className="w-3 h-3" />
              {subCount} sub-agent{subCount > 1 ? 's' : ''}
            </span>
          </div>
        )}
      </div>

      {/* Handle at bottom for sub-agent connections */}
      {subCount > 0 && (
        <Handle
          type="source"
          position={Position.Bottom}
          className="!bg-primary/60 !border-primary/30 !w-2 !h-2"
        />
      )}
    </div>
  );
}
