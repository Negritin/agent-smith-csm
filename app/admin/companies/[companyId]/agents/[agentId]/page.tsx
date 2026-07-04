'use client';

import { Suspense, useEffect } from 'react';
import { useParams, useRouter, useSearchParams } from 'next/navigation';
import { useAdminRole } from '@/hooks/useAdminRole';
import { AgentConfigView } from '@/components/admin/agent-config/AgentConfigView';
import { InlineNotice, LoadingState } from '@/components/ui/feedback-state';

function AdminCompanyAgentConfigPageContent() {
  const { role, isLoading: roleLoading } = useAdminRole();
  const router = useRouter();
  const params = useParams();
  const searchParams = useSearchParams();
  const companyId = params.companyId as string;
  // 'new' cai no mesmo segmento dinâmico => modo criação (agentId ausente)
  const rawAgentId = params.agentId as string;
  const agentId = rawAgentId === 'new' ? undefined : rawAgentId;
  // Deep-link por seção via ?section=mcp (SPEC impl §5.1)
  const section = searchParams.get('section') ?? undefined;

  // Verificar permissão Super Admin
  useEffect(() => {
    if (!roleLoading && role !== 'master') {
      router.push('/admin');
    }
  }, [role, roleLoading, router]);

  if (roleLoading) {
    return <LoadingState />;
  }

  if (role !== 'master') {
    return (
      <InlineNotice tone="danger" className="m-8">
        Acesso negado. Apenas Super Admin pode acessar esta página.
      </InlineNotice>
    );
  }

  return (
    <AgentConfigView
      companyId={companyId}
      agentId={agentId}
      initialSection={section}
      onBack={() => router.push(`/admin/companies/${companyId}/agents`)}
      onSaved={(newAgentId) => router.push(`/admin/companies/${companyId}/agents/${newAgentId}`)}
    />
  );
}

export default function AdminCompanyAgentConfigPage() {
  return (
    <Suspense fallback={<LoadingState />}>
      <AdminCompanyAgentConfigPageContent />
    </Suspense>
  );
}
