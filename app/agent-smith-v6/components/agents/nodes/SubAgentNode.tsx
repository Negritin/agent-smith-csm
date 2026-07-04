'use client';

import { Handle, Position, type NodeProps, type Node } from '@xyflow/react';
import { Zap, Edit, Archive } from 'lucide-react';

type SubAgentNodeData = {
  agent: {
    id: string;
    name: string;
    slug?: string;
    is_active?: boolean;
    llm_provider?: string;
    llm_model?: string;
  };
  parentName: string | null;
  taskDescription: string;
  onEdit: (id: string) => void;
  onArchive: (id: string) => void;
};

export function SubAgentNode({ data }: NodeProps<Node<SubAgentNodeData>>) {
  const { agent, parentName, onEdit, onArchive } = data;

  const modelInfo = [agent.llm_provider, agent.llm_model].filter(Boolean).join(' · ');

  return (
    <div className="sub-agent-node group" style={{ width: 260 }}>
      {/* Handle at top for parent connection */}
      <Handle
        type="target"
        position={Position.Top}
        className="!bg-primary/60 !border-primary/30 !w-2 !h-2"
      />

      <div
        className="
          dark relative rounded-lg border border-border bg-card shadow-[var(--shadow-border)]
          transition-all duration-300
          group-hover:border-primary/30 group-hover:shadow-[var(--shadow-raised)]
        "
      >
        {/* Header */}
        <div className="px-3 pt-3 pb-2">
          <div className="flex items-start justify-between">
            <div className="flex items-center gap-2">
              <div className="p-1.5 bg-accent/10 rounded-md border border-accent/20">
                <Zap className="w-3.5 h-3.5 text-accent" />
              </div>
              <div>
                <h4 className="text-[13px] font-medium text-foreground leading-tight">
                  {agent.name}
                </h4>
                {parentName && (
                  <p className="text-[10px] text-muted-foreground mt-0.5">
                    Sub-agente de {parentName}
                  </p>
                )}
              </div>
            </div>
            {/* Status dot */}
            <span
              className={`w-2 h-2 rounded-full mt-1.5 ${
                agent.is_active !== false
                  ? 'bg-success shadow-sm shadow-success/30'
                  : 'bg-muted-foreground/40'
              }`}
            />
          </div>
        </div>

        {/* Model info condensed */}
        {modelInfo && (
          <div className="px-3 pb-2">
            <p className="text-[11px] text-muted-foreground truncate">{modelInfo}</p>
          </div>
        )}

        {/* Actions */}
        <div className="px-3 pb-2.5 flex gap-1.5 border-t border-border pt-2">
          <button
            onClick={() => onEdit(agent.id)}
            className="
              flex-1 flex items-center justify-center gap-1 text-[11px] font-medium
              py-1 rounded-md
              bg-brand-muted border border-primary/20 text-white
              hover:bg-primary/10 hover:border-primary/30
              transition-all duration-200 cursor-pointer
            "
          >
            <Edit className="w-2.5 h-2.5" />
            Editar
          </button>
          <button
            onClick={() => onArchive(agent.id)}
            className="
              flex items-center justify-center gap-1 text-[11px] font-medium
              py-1 px-2 rounded-md
              bg-danger/10 border border-danger/20 text-danger
              hover:bg-danger/10 hover:border-danger/20
              transition-all duration-200 cursor-pointer
            "
          >
            <Archive className="w-2.5 h-2.5" />
          </button>
        </div>
      </div>
    </div>
  );
}
