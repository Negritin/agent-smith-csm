'use client';

import { useEffect } from 'react';
import { Button } from '@/components/ui/button';
import { AlertCircle } from 'lucide-react';

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <html>
      <body>
        <div className="flex h-screen w-full flex-col items-center justify-center gap-4 bg-background text-foreground">
          <div className="flex flex-col items-center gap-2 text-center">
            <AlertCircle className="h-10 w-10 text-destructive" />
            <h2 className="text-2xl font-bold tracking-tight">Algo deu errado!</h2>
            <p className="text-muted-foreground">
              Ocorreu um erro inesperado. Nossa equipe já foi notificada.
            </p>
          </div>
          <Button onClick={() => reset()}>Tentar novamente</Button>
        </div>
      </body>
    </html>
  );
}
