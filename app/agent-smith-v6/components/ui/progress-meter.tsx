import * as React from 'react';

import { cn } from '@/lib/utils';

type ProgressTone = 'brand' | 'success' | 'warning' | 'danger' | 'info' | 'accent';

const toneClasses: Record<ProgressTone, string> = {
  brand: 'bg-primary',
  success: 'bg-success',
  warning: 'bg-warning',
  danger: 'bg-danger',
  info: 'bg-info',
  accent: 'bg-accent',
};

interface ProgressMeterProps extends React.HTMLAttributes<HTMLDivElement> {
  value: number;
  max?: number;
  tone?: ProgressTone;
  label?: React.ReactNode;
  caption?: React.ReactNode;
}

export function ProgressMeter({
  value,
  max = 100,
  tone = 'brand',
  label,
  caption,
  className,
  ...props
}: ProgressMeterProps) {
  const percent = max > 0 ? Math.min(100, Math.max(0, (value / max) * 100)) : 0;

  return (
    <div className={cn('space-y-2', className)} {...props}>
      {(label || caption) && (
        <div className="flex items-center justify-between gap-3 text-sm">
          {label && <span className="font-medium text-foreground">{label}</span>}
          {caption && <span className="text-muted-foreground">{caption}</span>}
        </div>
      )}
      <div className="h-2 overflow-hidden rounded-full bg-muted">
        <div
          className={cn('h-full rounded-full transition-[width]', toneClasses[tone])}
          style={{ width: `${percent}%` }}
        />
      </div>
    </div>
  );
}
