'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Users } from 'lucide-react';

interface Props {
  name: string;
  setName: (value: string) => void;
  slug: string;
  setSlug: (value: string) => void;
  isSubagent: boolean;
  setIsSubagent: (value: boolean) => void;
  allowDirectChat: boolean;
  setAllowDirectChat: (value: boolean) => void;
}

export function IdentitySection({
  name,
  setName,
  slug,
  setSlug,
  isSubagent,
  setIsSubagent,
  allowDirectChat,
  setAllowDirectChat,
}: Props) {
  return (
    <div className="space-y-6">
      <Card className="bg-card border-border">
        <CardHeader>
          <CardTitle className="text-sm text-foreground">Identificação do Agente</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div>
            <Label htmlFor="name" className="text-muted-foreground">
              Nome do Agente *
            </Label>
            <Input
              id="name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Ex: Agente de Vendas"
              className="bg-background border-border text-foreground"
            />
            <p className="text-xs text-muted-foreground mt-1">
              Nome descritivo para identificar o agente
            </p>
          </div>

          <div>
            <Label htmlFor="slug" className="text-muted-foreground">
              Slug (Identificador) *
            </Label>
            <Input
              id="slug"
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              placeholder="Ex: agente-de-vendas"
              className="bg-background border-border text-foreground font-mono"
            />
            <p className="text-xs text-muted-foreground mt-1">
              Identificador único (auto-gerado do nome, editável)
            </p>
          </div>
        </CardContent>
      </Card>

      {/* SubAgent Configuration */}
      <Card className="bg-card border-border">
        <CardHeader>
          <CardTitle className="text-sm text-foreground flex items-center gap-2">
            <Users className="w-4 h-4 text-primary" />
            Configuração de SubAgent
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center justify-between">
            <div className="space-y-0.5">
              <Label className="text-foreground">Marcar como Especialista (SubAgent)</Label>
              <p className="text-xs text-muted-foreground">
                Especialistas são acionados por orquestradores. Oculta Widget e WhatsApp.
              </p>
            </div>
            <Switch checked={isSubagent} onCheckedChange={setIsSubagent} />
          </div>
          {isSubagent && (
            <div className="flex items-center justify-between animate-in fade-in slide-in-from-top-1">
              <div className="space-y-0.5">
                <Label className="text-foreground">Permitir Chat Direto (Debug)</Label>
                <p className="text-xs text-muted-foreground">
                  Exibe este agente no Chat Test do admin para testes e treinamento.
                </p>
              </div>
              <Switch checked={allowDirectChat} onCheckedChange={setAllowDirectChat} />
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
