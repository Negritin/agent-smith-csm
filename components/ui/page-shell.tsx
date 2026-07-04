import * as React from 'react';

import { cn } from '@/lib/utils';

type PageShellProps = React.HTMLAttributes<HTMLDivElement> & {
  size?: 'default' | 'wide' | 'full';
};

const shellSize = {
  default: 'max-w-7xl',
  wide: 'max-w-[1440px]',
  full: 'max-w-none',
};

export function PageShell({ className, size = 'wide', ...props }: PageShellProps) {
  return (
    <div
      className={cn(
        'mx-auto flex w-full flex-col gap-6 px-[var(--space-page-x)] py-[var(--space-page-y)]',
        shellSize[size],
        className,
      )}
      {...props}
    />
  );
}

export function PageHeader({
  className,
  children,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <header
      className={cn(
        'flex flex-col gap-4 border-b border-border/80 pb-5 md:flex-row md:items-end md:justify-between',
        className,
      )}
      {...props}
    >
      {children}
    </header>
  );
}

export function PageTitle({ className, ...props }: React.HTMLAttributes<HTMLHeadingElement>) {
  return (
    <h1
      className={cn('text-2xl font-semibold leading-8 tracking-normal text-foreground', className)}
      {...props}
    />
  );
}

export function PageDescription({
  className,
  ...props
}: React.HTMLAttributes<HTMLParagraphElement>) {
  return (
    <p className={cn('max-w-3xl text-sm leading-6 text-muted-foreground', className)} {...props} />
  );
}

export function PageActions({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={cn('flex flex-wrap items-center gap-2 md:justify-end', className)} {...props} />
  );
}

export function PageToolbar({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        'flex flex-col gap-3 rounded-lg border border-border bg-card p-3 shadow-[var(--shadow-border)] md:flex-row md:items-center md:justify-between',
        className,
      )}
      {...props}
    />
  );
}

export function PageSection({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <section className={cn('flex flex-col gap-4', className)} {...props} />;
}
