'use client';

import { UCPConfigTab } from '@/components/admin/UCPConfigTab';
import { Card, CardContent } from '@/components/ui/card';

interface Props {
  agentId?: string;
  companyId: string;
}

export function CommerceSection({ agentId, companyId }: Props) {
  return (
    <div className="space-y-6">
      {!agentId ? (
        <Card className="bg-brand-muted border-primary/30">
          <CardContent className="pt-4">
            <p className="text-primary text-sm">
              Salve o agente primeiro antes de configurar integrações de comércio.
            </p>
          </CardContent>
        </Card>
      ) : (
        <UCPConfigTab agentId={agentId} companyId={companyId} />
      )}
    </div>
  );
}
