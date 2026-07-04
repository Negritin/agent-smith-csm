'use client';

import { useMemo } from 'react';
import type { Node, Edge } from '@xyflow/react';
import { Agent } from '@/types/agent';

// ── Layout Constants ──
const MAIN_NODE_WIDTH = 320;
const SUB_NODE_WIDTH = 260;
const HORIZONTAL_GAP = 100;
const VERTICAL_GAP = 340;
const SUB_HORIZONTAL_GAP = 40;

export interface DelegatedSubAgent {
  subagent_id: string;
  subagent_name: string;
  task_description: string;
}

export interface AgentWithDelegations extends Agent {
  delegated_sub_agents: DelegatedSubAgent[];
}

export function useAgentFlowLayout(
  agents: AgentWithDelegations[],
  onEdit: (agentId: string) => void,
  onArchive: (agentId: string) => void,
) {
  return useMemo(() => {
    const nodes: Node[] = [];
    const edges: Edge[] = [];

    if (!agents || agents.length === 0) return { nodes, edges };

    // Separate main agents (NOT sub-agents) from sub-agents
    const mainAgents = agents.filter((a) => !a.is_subagent);
    const agentLookup = new Map(agents.map((a) => [a.id, a]));

    let currentX = 0;

    for (const agent of mainAgents) {
      const subs = agent.delegated_sub_agents || [];
      const subCount = subs.length;

      // Group width: determined by sub-agents or main node width
      const subsWidth =
        subCount > 0 ? subCount * SUB_NODE_WIDTH + (subCount - 1) * SUB_HORIZONTAL_GAP : 0;
      const groupWidth = Math.max(MAIN_NODE_WIDTH, subsWidth);

      // Center main node in the group
      const mainX = currentX + (groupWidth - MAIN_NODE_WIDTH) / 2;

      // Main agent node
      nodes.push({
        id: agent.id,
        type: 'mainAgent',
        position: { x: mainX, y: 0 },
        data: {
          agent,
          subCount,
          onEdit,
          onArchive,
        },
      });

      // Sub-agent nodes
      if (subCount > 0) {
        const subsStartX = currentX + (groupWidth - subsWidth) / 2;

        subs.forEach((sub, i) => {
          const subAgent = agentLookup.get(sub.subagent_id);
          const subX = subsStartX + i * (SUB_NODE_WIDTH + SUB_HORIZONTAL_GAP);

          nodes.push({
            id: sub.subagent_id,
            type: 'subAgent',
            position: { x: subX, y: VERTICAL_GAP },
            data: {
              agent: subAgent || {
                id: sub.subagent_id,
                name: sub.subagent_name,
              },
              parentName: agent.name,
              taskDescription: sub.task_description,
              onEdit,
              onArchive,
            },
          });

          // Edge connecting the parent agent to the subagent.
          edges.push({
            id: `e-${agent.id}-${sub.subagent_id}`,
            source: agent.id,
            target: sub.subagent_id,
            type: 'glowAnimated',
          });
        });
      }

      currentX += groupWidth + HORIZONTAL_GAP;
    }

    // Also add standalone sub-agents that have no parent delegation
    // (orphan sub-agents still show as standalone nodes)
    const subAgentIdsWithParent = new Set(
      mainAgents.flatMap((a) => (a.delegated_sub_agents || []).map((s) => s.subagent_id)),
    );
    const orphanSubs = agents.filter((a) => a.is_subagent && !subAgentIdsWithParent.has(a.id));

    for (const orphan of orphanSubs) {
      nodes.push({
        id: orphan.id,
        type: 'subAgent',
        position: { x: currentX, y: 0 },
        data: {
          agent: orphan,
          parentName: null,
          taskDescription: '',
          onEdit,
          onArchive,
        },
      });
      currentX += SUB_NODE_WIDTH + HORIZONTAL_GAP;
    }

    return { nodes, edges };
  }, [agents, onEdit, onArchive]);
}
