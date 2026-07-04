import * as React from 'react';
import type { LucideIcon } from 'lucide-react';

import { cn } from '@/lib/utils';
import { Input } from '@/components/ui/input';

export function FilterBar({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        'flex flex-col gap-3 rounded-lg border border-border bg-card p-3 shadow-[var(--shadow-border)] lg:flex-row lg:items-center lg:justify-between',
        className,
      )}
      {...props}
    />
  );
}

export function FilterGroup({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn('flex flex-1 flex-col gap-3 sm:flex-row sm:items-center', className)}
      {...props}
    />
  );
}

export function FilterActions({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('flex flex-wrap items-center gap-2', className)} {...props} />;
}

interface SearchFieldProps extends React.InputHTMLAttributes<HTMLInputElement> {
  icon?: LucideIcon;
  wrapperClassName?: string;
}

export const SearchField = React.forwardRef<HTMLInputElement, SearchFieldProps>(
  ({ className, wrapperClassName, icon: Icon, ...props }, ref) => (
    <div className={cn('relative min-w-0 flex-1', wrapperClassName)}>
      {Icon && (
        <Icon className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
      )}
      <Input ref={ref} className={cn(Icon && 'pl-10', className)} {...props} />
    </div>
  ),
);
SearchField.displayName = 'SearchField';

export function FilterSummary({ className, ...props }: React.HTMLAttributes<HTMLParagraphElement>) {
  return <p className={cn('text-sm text-muted-foreground', className)} {...props} />;
}
