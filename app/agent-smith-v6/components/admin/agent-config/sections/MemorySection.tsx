'use client';

import { MemoryConfigTab } from '@/components/admin/MemoryConfigTab';

interface Props {
  agentId?: string;
}

export function MemorySection({ agentId }: Props) {
  return (
    <div className="space-y-6">
      <MemoryConfigTab agentId={agentId || ''} />
    </div>
  );
}
