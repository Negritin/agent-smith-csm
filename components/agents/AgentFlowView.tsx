'use client';

import {
  ReactFlow,
  Background,
  BackgroundVariant,
  Controls,
  PanOnScrollMode,
  type Node,
  type Edge,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { useTheme } from 'next-themes';

import { MainAgentNode } from './nodes/MainAgentNode';
import { SubAgentNode } from './nodes/SubAgentNode';
import { GlowAnimatedEdge } from './edges/GlowAnimatedEdge';
import { useAgentFlowLayout, type AgentWithDelegations } from './hooks/useAgentFlowLayout';

const nodeTypes = {
  mainAgent: MainAgentNode,
  subAgent: SubAgentNode,
};

const edgeTypes = {
  glowAnimated: GlowAnimatedEdge,
};

interface AgentFlowViewProps {
  agents: AgentWithDelegations[];
  onEdit: (agentId: string) => void;
  onArchive: (agentId: string) => void;
}

export function AgentFlowView({ agents, onEdit, onArchive }: AgentFlowViewProps) {
  const { nodes, edges } = useAgentFlowLayout(agents, onEdit, onArchive);
  // Controles/chrome do React Flow seguem o tema do app (antes era `light` fixo,
  // deixando os botões +/−/reset brancos no dark mode).
  const { resolvedTheme } = useTheme();

  return (
    <div
      className="smith-canvas-grid w-full rounded-xl border border-border overflow-hidden"
      style={{
        height: 'calc(100vh - 260px)',
        minHeight: 420,
      }}
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        colorMode={resolvedTheme === 'dark' ? 'dark' : 'light'}
        fitView
        fitViewOptions={{ padding: 0.35, maxZoom: 1.2 }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={true}
        panOnScroll={true}
        panOnScrollMode={PanOnScrollMode.Horizontal}
        zoomOnScroll={false}
        minZoom={0.4}
        maxZoom={1.5}
        proOptions={{ hideAttribution: true }}
      >
        <Background
          color="hsl(var(--canvas-grid) / 0.34)"
          gap={56}
          variant={BackgroundVariant.Lines}
        />
        <Controls
          showInteractive={false}
          className="!bg-card !border-border !shadow-lg !rounded-lg"
        />

        {/* SVG defs for glow effects on edges */}
        <svg style={{ position: 'absolute', width: 0, height: 0 }}>
          <defs>
            <filter id="glow">
              <feGaussianBlur stdDeviation="3" result="coloredBlur" />
              <feMerge>
                <feMergeNode in="coloredBlur" />
                <feMergeNode in="SourceGraphic" />
              </feMerge>
            </filter>
            <filter id="edge-blur">
              <feGaussianBlur stdDeviation="4" />
            </filter>
            <linearGradient id="edge-gradient" x1="0%" y1="0%" x2="0%" y2="100%">
              <stop offset="0%" stopColor="hsl(var(--primary))" stopOpacity="0.75" />
              <stop offset="50%" stopColor="hsl(var(--accent))" stopOpacity="0.55" />
              <stop offset="100%" stopColor="hsl(var(--primary))" stopOpacity="0.35" />
            </linearGradient>
          </defs>
        </svg>
      </ReactFlow>
    </div>
  );
}
