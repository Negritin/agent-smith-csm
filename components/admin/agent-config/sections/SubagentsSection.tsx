'use client';

import { SubAgentConfigTab } from '@/components/admin/SubAgentConfigTab';

interface Props {
  agentId: string;
  companyId: string;
}

export function SubagentsSection({ agentId, companyId }: Props) {
  return (
    <div className="space-y-6">
      <SubAgentConfigTab agentId={agentId} companyId={companyId} />
    </div>
  );
}
