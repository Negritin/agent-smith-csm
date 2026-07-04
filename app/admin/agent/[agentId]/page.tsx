'use client';

import { Suspense, useEffect } from 'react';
import { useParams, useRouter, useSearchParams } from 'next/navigation';
import { useAdminRole } from '@/hooks/useAdminRole';
import { AgentConfigView } from '@/components/admin/agent-config/AgentConfigView';
import { InlineNotice, LoadingState } from '@/components/ui/feedback-state';

function AgentConfigEditPageContent() {
  const { role, companyId, isLoading } = useAdminRole();
  const router = useRouter();
  const params = useParams();
  const searchParams = useSearchParams();
  const agentId = params.agentId as string;
  // Deep-link por seção via ?section=mcp (SPEC impl §5.1)
  const section = searchParams.get('section') ?? undefined;

  useEffect(() => {
    if (!isLoading && role !== 'company_admin') {
      router.push('/admin');
    }
  }, [role, isLoading, router]);

  if (isLoading) {
    return <LoadingState />;
  }

  if (!companyId) {
    return (
      <InlineNotice tone="danger" className="m-8">
        Erro: Empresa não encontrada
      </InlineNotice>
    );
  }

  return (
    <AgentConfigView
      companyId={companyId}
      agentId={agentId}
      initialSection={section}
      onBack={() => router.push('/admin/agent')}
    />
  );
}

export default function AgentConfigEditPage() {
  return (
    <Suspense fallback={<LoadingState />}>
      <AgentConfigEditPageContent />
    </Suspense>
  );
}
