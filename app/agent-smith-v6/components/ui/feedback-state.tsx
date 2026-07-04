import * as React from 'react';
import type { LucideIcon } from 'lucide-react';
import { AlertCircle, Inbox, Loader2 } from 'lucide-react';

import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';

interface StateBlockProps extends React.HTMLAttributes<HTMLDivElement> {
  title: string;
  description?: React.ReactNode;
  icon?: LucideIcon;
  action?: React.ReactNode;
}

export function EmptyStatePanel({
  title,
  description,
  icon: Icon = Inbox,
  action,
  className,
  ...props
}: StateBlockProps) {
  return (
    <div
      className={cn(
        'flex min-h-[220px] flex-col items-center justify-center rounded-lg border border-dashed border-border bg-card px-6 py-10 text-center',
        className,
      )}
      {...props}
    >
      <div className="mb-4 rounded-md border border-border bg-muted p-3 text-muted-foreground">
        <Icon className="h-5 w-5" />
      </div>
      <h3 className="text-base font-semibold text-foreground">{title}</h3>
      {description && (
        <p className="mt-2 max-w-md text-sm leading-6 text-muted-foreground">{description}</p>
      )}
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}

export function ErrorStatePanel({
  title,
  description,
  action,
  className,
  ...props
}: Omit<StateBlockProps, 'icon'>) {
  return (
    <EmptyStatePanel
      title={title}
      description={description}
      icon={AlertCircle}
      action={action}
      className={cn('border-danger/30 bg-danger/5', className)}
      {...props}
    />
  );
}

interface LoadingStateProps extends React.HTMLAttributes<HTMLDivElement> {
  label?: string;
}

export function LoadingState({ label = 'Carregando...', className, ...props }: LoadingStateProps) {
  return (
    <div
      className={cn(
        'flex min-h-[220px] flex-col items-center justify-center gap-3 text-muted-foreground',
        className,
      )}
      {...props}
    >
      <Loader2 className="h-5 w-5 animate-spin text-primary" />
      <p className="text-sm font-medium">{label}</p>
    </div>
  );
}

interface InlineNoticeProps extends React.HTMLAttributes<HTMLDivElement> {
  tone?: 'brand' | 'success' | 'warning' | 'danger' | 'info' | 'neutral';
}

const noticeTone = {
  brand: 'border-primary/20 bg-brand-muted text-primary',
  success: 'border-success/20 bg-success/10 text-success',
  warning: 'border-warning/25 bg-warning/10 text-warning',
  danger: 'border-danger/20 bg-danger/10 text-danger',
  info: 'border-info/20 bg-info/10 text-info',
  neutral: 'border-border bg-muted text-muted-foreground',
};

export function InlineNotice({ tone = 'neutral', className, ...props }: InlineNoticeProps) {
  return (
    <div
      className={cn('rounded-lg border px-4 py-3 text-sm font-medium', noticeTone[tone], className)}
      {...props}
    />
  );
}

export function RetryButton({ onClick }: { onClick?: () => void }) {
  return (
    <Button variant="outline" onClick={onClick}>
      Tentar novamente
    </Button>
  );
}
