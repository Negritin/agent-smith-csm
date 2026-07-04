'use client';

import { getSmoothStepPath, type EdgeProps, type Edge, BaseEdge } from '@xyflow/react';

export type GlowAnimatedEdgeData = Record<string, unknown>;

export function GlowAnimatedEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
}: EdgeProps<Edge<GlowAnimatedEdgeData>>) {
  const [edgePath] = getSmoothStepPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
    borderRadius: 16,
  });

  return (
    <g className="react-flow__edge-glow">
      <path d={edgePath} fill="none" stroke="hsl(var(--border))" strokeWidth={6} opacity={0.45} />

      <BaseEdge
        id={id}
        path={edgePath}
        style={{
          stroke: 'url(#edge-gradient)',
          strokeWidth: 2,
        }}
      />

      {[...Array(2)].map((_, i) => (
        <g key={i}>
          <circle r="2" fill="hsl(var(--primary))" opacity={0.65}>
            <animateMotion
              begin={`${i * 1.2}s`}
              dur="4s"
              repeatCount="indefinite"
              path={edgePath}
              calcMode="spline"
              keySplines="0.42, 0, 0.58, 1.0"
            />
          </circle>
        </g>
      ))}
    </g>
  );
}
