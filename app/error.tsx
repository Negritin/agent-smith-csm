'use client';

import { useEffect } from 'react';
import { Button } from '@/components/ui/button';
import { AlertCircle } from 'lucide-react';

export default function ErrorBoundary({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error('App Error:', error);
  }, [error]);

  return (
    <div className="flex h-[50vh] w-full flex-col items-center justify-center gap-4 p-4">
      <div className="flex flex-col items-center gap-2 text-center">
        <AlertCircle className="h-8 w-8 text-destructive" />
        <h2 className="text-xl font-semibold">Ops, algo deu errado.</h2>
        <p className="text-sm text-muted-foreground max-w-md">
          {error.message || 'Um erro inesperado ocorreu. Tente recarregar a página.'}
        </p>
      </div>
      <Button variant="outline" onClick={() => reset()}>
        Tentar novamente
      </Button>
    </div>
  );
}
