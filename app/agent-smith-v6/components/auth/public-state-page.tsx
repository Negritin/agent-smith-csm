import * as React from 'react';
import type { LucideIcon } from 'lucide-react';

import { AuthCard, AuthShell } from '@/components/auth/auth-shell';
import { cn } from '@/lib/utils';

type Tone = 'brand' | 'success' | 'warning' | 'danger' | 'info';

const toneClass: Record<Tone, string> = {
  brand: 'bg-brand-muted text-primary',
  success: 'bg-success/10 text-success',
  warning: 'bg-warning/10 text-warning',
  danger: 'bg-danger/10 text-danger',
  info: 'bg-info/10 text-info',
};

type PublicStatePageProps = {
  title: string;
  description: React.ReactNode;
  icon: LucideIcon;
  tone?: Tone;
  notice?: React.ReactNode;
  children?: React.ReactNode;
  actions?: React.ReactNode;
};

export function PublicStatePage({
  title,
  description,
  icon: Icon,
  tone = 'brand',
  notice,
  children,
  actions,
}: PublicStatePageProps) {
  return (
    <AuthShell size="md">
      <AuthCard
        title={title}
        description={description}
        logoHref="/"
        headerAction={
          <div
            className={cn(
              'mt-1 flex h-14 w-14 items-center justify-center rounded-lg',
              toneClass[tone],
            )}
          >
            <Icon className="h-7 w-7" />
          </div>
        }
        contentClassName="space-y-6"
      >
        {notice}
        {children}
        {actions && <div className="space-y-3 pt-1">{actions}</div>}
      </AuthCard>
    </AuthShell>
  );
}
