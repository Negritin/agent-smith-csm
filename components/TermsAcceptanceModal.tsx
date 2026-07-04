'use client';

import { useState } from 'react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { FileText, Loader2 } from 'lucide-react';

interface ActiveTerms {
  id: string;
  title: string;
  content: string;
  version: string;
}

interface TermsAcceptanceModalProps {
  activeTerms: ActiveTerms;
  onAccepted: () => void;
}

export function TermsAcceptanceModal({ activeTerms, onAccepted }: TermsAcceptanceModalProps) {
  const [accepting, setAccepting] = useState(false);

  const handleAccept = async () => {
    setAccepting(true);
    try {
      const response = await fetch('/api/user/accept-terms', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ documentId: activeTerms.id }),
      });

      if (response.ok) {
        onAccepted();
      } else {
        console.error('Failed to accept terms');
        setAccepting(false);
      }
    } catch {
      console.error('Error accepting terms');
      setAccepting(false);
    }
  };

  return (
    <Dialog open={true} onOpenChange={() => {}}>
      <DialogContent
        className="max-w-2xl max-h-[90vh] overflow-hidden flex flex-col [&>button]:hidden"
        onPointerDownOutside={(e) => e.preventDefault()}
        onEscapeKeyDown={(e) => e.preventDefault()}
        onInteractOutside={(e) => e.preventDefault()}
      >
        <DialogHeader className="flex-shrink-0">
          <DialogTitle className="text-xl flex items-center gap-2">
            <FileText className="w-5 h-5 text-primary" />
            Termos de Uso Atualizados
          </DialogTitle>
          <DialogDescription className="text-muted-foreground">
            Os Termos de Uso foram atualizados (v{activeTerms.version}). Leia e aceite para
            continuar.
          </DialogDescription>
        </DialogHeader>

        <div className="flex-1 overflow-y-auto my-4 bg-muted rounded-lg p-6 border border-border">
          <h3 className="text-lg font-semibold text-foreground mb-4">{activeTerms.title}</h3>
          <div className="whitespace-pre-wrap text-muted-foreground text-sm leading-relaxed">
            {activeTerms.content}
          </div>
        </div>

        <div className="flex-shrink-0 pt-2 border-t border-border">
          <Button onClick={handleAccept} disabled={accepting} className="w-full py-3 text-base">
            {accepting ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                Processando...
              </>
            ) : (
              'Li e aceito os Termos de Uso'
            )}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
