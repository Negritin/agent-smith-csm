'use client';

import { Suspense, useEffect } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { useAdminRole } from '@/hooks/useAdminRole';
import { AgentConfigView } from '@/components/admin/agent-config/AgentConfigView';
import { InlineNotice, LoadingState } from '@/components/ui/feedback-state';

function AgentConfigCreatePageContent() {
  const { role, companyId, isLoading } = useAdminRole();
  const router = useRouter();
  const searchParams = useSearchParams();
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
      initialSection={section}
      onBack={() => router.push('/admin/agent')}
      // Após criar com sucesso, redireciona pra tela de edição (SPEC impl §5.1)
      onSaved={(newAgentId) => router.push(`/admin/agent/${newAgentId}`)}
    />
  );
}

export default function AgentConfigCreatePage() {
  return (
    <Suspense fallback={<LoadingState />}>
      <AgentConfigCreatePageContent />
    </Suspense>
  );
}
