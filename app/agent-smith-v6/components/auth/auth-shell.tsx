import * as React from 'react';
import Image from 'next/image';
import Link from 'next/link';

import { Card, CardContent, CardFooter, CardHeader } from '@/components/ui/card';
import { cn } from '@/lib/utils';

type AuthShellProps = React.HTMLAttributes<HTMLDivElement> & {
  size?: 'sm' | 'md' | 'lg';
};

const shellSize = {
  sm: 'max-w-md',
  md: 'max-w-lg',
  lg: 'max-w-2xl',
};

export function AuthShell({ children, className, size = 'sm', ...props }: AuthShellProps) {
  return (
    <main
      className={cn(
        'relative flex min-h-screen items-center justify-center overflow-hidden bg-background px-4 py-8 text-foreground',
        className,
      )}
      {...props}
    >
      <div className="pointer-events-none absolute inset-0 bg-[linear-gradient(hsl(var(--border)/0.55)_1px,transparent_1px),linear-gradient(90deg,hsl(var(--border)/0.55)_1px,transparent_1px)] bg-[size:44px_44px] opacity-35" />
      <div className="pointer-events-none absolute inset-x-0 top-0 h-48 bg-[linear-gradient(180deg,hsl(var(--brand-muted)),transparent)] opacity-70" />
      <div className={cn('relative z-10 w-full', shellSize[size])}>{children}</div>
    </main>
  );
}

type AuthCardProps = React.HTMLAttributes<HTMLDivElement> & {
  title: string;
  description?: React.ReactNode;
  logoHref?: string;
  footer?: React.ReactNode;
  headerAction?: React.ReactNode;
  contentClassName?: string;
};

export function AuthCard({
  title,
  description,
  logoHref = '/landing',
  footer,
  headerAction,
  contentClassName,
  children,
  className,
  ...props
}: AuthCardProps) {
  return (
    <Card className={cn('overflow-hidden shadow-[var(--shadow-raised)]', className)} {...props}>
      <CardHeader className="items-center gap-4 px-6 pb-5 pt-7 text-center">
        <Link href={logoHref} className="rounded-md transition-opacity hover:opacity-85">
          <Image
            src="/smith-logo.png"
            alt="Smith Logo"
            width={56}
            height={56}
            className="h-14 w-14"
            priority
          />
        </Link>
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold leading-8 text-foreground">{title}</h1>
          {description && (
            <p className="mx-auto max-w-sm text-sm leading-6 text-muted-foreground">
              {description}
            </p>
          )}
        </div>
        {headerAction}
      </CardHeader>
      <CardContent className={cn('px-6 pb-6', contentClassName)}>{children}</CardContent>
      {footer && (
        <CardFooter className="border-t border-border bg-muted/35 px-6 py-4">{footer}</CardFooter>
      )}
    </Card>
  );
}
