import * as React from 'react';

import { cn } from '@/lib/utils';

export function ObjectList({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn('divide-y divide-border rounded-lg border border-border bg-card', className)}
      {...props}
    />
  );
}

export function ObjectListItem({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        'flex items-start justify-between gap-4 p-4 transition-colors hover:bg-brand-subtle',
        className,
      )}
      {...props}
    />
  );
}

export function ObjectListTitle({ className, ...props }: React.HTMLAttributes<HTMLHeadingElement>) {
  return (
    <h3 className={cn('text-sm font-semibold leading-5 text-foreground', className)} {...props} />
  );
}

export function ObjectListMeta({
  className,
  ...props
}: React.HTMLAttributes<HTMLParagraphElement>) {
  return <p className={cn('text-sm leading-5 text-muted-foreground', className)} {...props} />;
}

export function ObjectListActions({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('flex shrink-0 items-center gap-2', className)} {...props} />;
}
