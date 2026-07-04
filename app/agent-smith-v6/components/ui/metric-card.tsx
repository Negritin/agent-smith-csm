import * as React from 'react';
import type { LucideIcon } from 'lucide-react';

import { cn } from '@/lib/utils';

type MetricTone = 'brand' | 'neutral' | 'success' | 'warning' | 'danger' | 'info';

const toneClass: Record<MetricTone, string> = {
  brand: 'text-primary bg-brand-muted border-primary/15',
  neutral: 'text-muted-foreground bg-muted border-border',
  success: 'text-success bg-success/10 border-success/20',
  warning: 'text-warning bg-warning/10 border-warning/25',
  danger: 'text-danger bg-danger/10 border-danger/20',
  info: 'text-info bg-info/10 border-info/20',
};

interface MetricCardProps extends React.HTMLAttributes<HTMLDivElement> {
  label: string;
  value: React.ReactNode;
  description?: React.ReactNode;
  trend?: React.ReactNode;
  icon?: LucideIcon;
  tone?: MetricTone;
}

export function MetricCard({
  label,
  value,
  description,
  trend,
  icon: Icon,
  tone = 'neutral',
  className,
  ...props
}: MetricCardProps) {
  return (
    <div
      className={cn(
        'rounded-xl border border-border/50 bg-card p-5 shadow-[var(--shadow-raised)]',
        className,
      )}
      {...props}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="text-sm font-medium text-muted-foreground">{label}</p>
          <div className="mt-2 flex flex-wrap items-baseline gap-2">
            <p className="text-2xl font-semibold leading-8 text-foreground">{value}</p>
            {trend && <span className="text-sm font-semibold text-success">{trend}</span>}
          </div>
        </div>
        {Icon && (
          <span className={cn('rounded-md border p-2', toneClass[tone])}>
            <Icon className="h-4 w-4" />
          </span>
        )}
      </div>
      {description && <p className="mt-3 text-sm text-muted-foreground">{description}</p>}
    </div>
  );
}
