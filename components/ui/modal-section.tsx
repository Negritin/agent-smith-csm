import * as React from 'react';

import { cn } from '@/lib/utils';

export function ModalSection({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <section className={cn('rounded-lg border border-border bg-card p-4', className)} {...props} />
  );
}

export function ModalSectionHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={cn('mb-4 flex items-start justify-between gap-3', className)} {...props} />
  );
}

export function ModalSectionTitle({
  className,
  ...props
}: React.HTMLAttributes<HTMLHeadingElement>) {
  return (
    <h3 className={cn('text-sm font-semibold leading-5 text-foreground', className)} {...props} />
  );
}

export function ModalSectionDescription({
  className,
  ...props
}: React.HTMLAttributes<HTMLParagraphElement>) {
  return <p className={cn('mt-1 text-sm leading-5 text-muted-foreground', className)} {...props} />;
}
