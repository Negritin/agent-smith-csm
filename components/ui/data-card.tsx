import * as React from 'react';
import type { LucideIcon } from 'lucide-react';

import { cn } from '@/lib/utils';

export function DataCard({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <article
      className={cn(
        'rounded-xl border border-border/50 bg-card shadow-[var(--shadow-raised)] transition-colors hover:border-primary/25',
        className,
      )}
      {...props}
    />
  );
}

export function DataCardHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        'flex flex-col gap-4 border-b border-border/80 p-4 sm:flex-row sm:items-start sm:justify-between',
        className,
      )}
      {...props}
    />
  );
}

export function DataCardIdentity({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('flex min-w-0 items-start gap-3', className)} {...props} />;
}

interface DataCardIconProps extends React.HTMLAttributes<HTMLDivElement> {
  icon: LucideIcon;
  tone?: 'brand' | 'success' | 'warning' | 'danger' | 'neutral';
}

const iconToneClasses = {
  brand: 'border-primary/15 bg-brand-muted text-primary',
  success: 'border-success/20 bg-success/10 text-success',
  warning: 'border-warning/25 bg-warning/10 text-warning',
  danger: 'border-danger/20 bg-danger/10 text-danger',
  neutral: 'border-border bg-muted text-muted-foreground',
};

export function DataCardIcon({
  icon: Icon,
  tone = 'brand',
  className,
  ...props
}: DataCardIconProps) {
  return (
    <div
      className={cn(
        'flex h-11 w-11 shrink-0 items-center justify-center rounded-lg border',
        iconToneClasses[tone],
        className,
      )}
      {...props}
    >
      <Icon className="h-5 w-5" />
    </div>
  );
}

export function DataCardTitle({ className, ...props }: React.HTMLAttributes<HTMLHeadingElement>) {
  return (
    <h3
      className={cn('truncate text-base font-semibold leading-6 text-foreground', className)}
      {...props}
    />
  );
}

export function DataCardMeta({ className, ...props }: React.HTMLAttributes<HTMLParagraphElement>) {
  return <p className={cn('text-sm leading-5 text-muted-foreground', className)} {...props} />;
}

export function DataCardBadges({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('flex flex-wrap items-center gap-2', className)} {...props} />;
}

export function DataCardBody({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('p-4', className)} {...props} />;
}

export function DataFieldGrid({ className, ...props }: React.HTMLAttributes<HTMLDListElement>) {
  return (
    <dl
      className={cn('grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4', className)}
      {...props}
    />
  );
}

interface DataFieldProps extends React.HTMLAttributes<HTMLDivElement> {
  label: React.ReactNode;
  value: React.ReactNode;
}

export function DataField({ label, value, className, ...props }: DataFieldProps) {
  return (
    <div className={cn('min-w-0', className)} {...props}>
      <dt className="text-xs font-medium uppercase tracking-normal text-muted-foreground">
        {label}
      </dt>
      <dd className="mt-1 truncate text-sm font-medium text-foreground">{value}</dd>
    </div>
  );
}

export function DataCardActions({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('mt-4 flex flex-wrap items-center gap-2', className)} {...props} />;
}
