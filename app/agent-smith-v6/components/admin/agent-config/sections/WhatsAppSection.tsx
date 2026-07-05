'use client';

import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Check, Copy, Eye, EyeOff, Loader2, MessageCircle, RefreshCw } from 'lucide-react';

interface Props {
  isCreateMode: boolean;
  hasExistingIntegration: boolean;
  whatsappProvider: string;
  setWhatsappProvider: (value: string) => void;
  whatsappIdentifier: string;
  setWhatsappIdentifier: (value: string) => void;
  whatsappInstanceId: string;
  setWhatsappInstanceId: (value: string) => void;
  whatsappToken: string;
  setWhatsappToken: (value: string) => void;
  whatsappClientToken: string;
  setWhatsappClientToken: (value: string) => void;
  whatsappBaseUrl: string;
  setWhatsappBaseUrl: (value: string) => void;
  whatsappIsActive: boolean;
  setWhatsappIsActive: (value: boolean) => void;
  whatsappBufferEnabled: boolean;
  setWhatsappBufferEnabled: (value: boolean) => void;
  whatsappBufferDebounce: number;
  setWhatsappBufferDebounce: (value: number) => void;
  whatsappBufferMaxWait: number;
  setWhatsappBufferMaxWait: (value: number) => void;
  whatsappBusinessAccountId: string;
  setWhatsappBusinessAccountId: (value: string) => void;
  whatsappWebhookVerifyToken: string;
  setWhatsappWebhookVerifyToken: (value: string) => void;
  whatsappWebhookMode: 'shadow' | 'active';
  setWhatsappWebhookMode: (value: 'shadow' | 'active') => void;
  savingWhatsapp: boolean;
  onSaveWhatsapp: () => void;
  // Token de webhook por-integração (Fase 1: exibição read-only, copiar
  // desabilitado; regenerar disponível). Ver SPEC §4.2.
  whatsappWebhookToken: string;
  webhookUrlBase: string;
  regeneratingWebhook: boolean;
  onRegenerateWebhookToken: () => void;
}

export function WhatsAppSection({
  isCreateMode,
  hasExistingIntegration,
  whatsappProvider,
  setWhatsappProvider,
  whatsappIdentifier,
  setWhatsappIdentifier,
  whatsappInstanceId,
  setWhatsappInstanceId,
  whatsappToken,
  setWhatsappToken,
  whatsappClientToken,
  setWhatsappClientToken,
  whatsappBaseUrl,
  setWhatsappBaseUrl,
  whatsappIsActive,
  setWhatsappIsActive,
  whatsappBufferEnabled,
  setWhatsappBufferEnabled,
  whatsappBufferDebounce,
  setWhatsappBufferDebounce,
  whatsappBufferMaxWait,
  setWhatsappBufferMaxWait,
  whatsappBusinessAccountId,
  setWhatsappBusinessAccountId,
  whatsappWebhookVerifyToken,
  setWhatsappWebhookVerifyToken,
  whatsappWebhookMode,
  setWhatsappWebhookMode,
  savingWhatsapp,
  onSaveWhatsapp,
  whatsappWebhookToken,
  webhookUrlBase,
  regeneratingWebhook,
  onRegenerateWebhookToken,
}: Props) {
  const [showWhatsappToken, setShowWhatsappToken] = useState(false);
  const [showWhatsappClientToken, setShowWhatsappClientToken] = useState(false);
  const [confirmRegenerate, setConfirmRegenerate] = useState(false);
  const [copiedWebhookUrl, setCopiedWebhookUrl] = useState(false);

  // URL do webhook por-integração. A base (NEXT_PUBLIC_API_URL) é montada
  // server-side no GET; se ausente/não-pública, webhookUrlBase vem vazio e a UI
  // mostra o aviso de configuração em vez de uma URL quebrada (SPEC §1.3/§4.3).
  const webhookUrl =
    webhookUrlBase && whatsappWebhookToken
      ? `${webhookUrlBase}/api/v1/webhook/${whatsappProvider}/${whatsappWebhookToken}`
      : '';

  return (
    <>
      <div className="space-y-6">
        {/* Aviso se for modo criação */}
        {isCreateMode && (
          <Card className="bg-brand-muted border-primary/30">
            <CardContent className="pt-4">
              <p className="text-primary text-sm">
                Salve o agente primeiro antes de configurar a integração WhatsApp.
              </p>
            </CardContent>
          </Card>
        )}

        {/* Provider Selection */}
        <Card className="bg-card border-border">
          <CardHeader>
            <CardTitle className="text-sm text-foreground flex items-center gap-2">
              <MessageCircle className="w-4 h-4 text-primary" />
              Integração WhatsApp
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div>
              <Label className="text-muted-foreground">Provedor</Label>
              <Select
                value={whatsappProvider}
                onValueChange={setWhatsappProvider}
                disabled={isCreateMode}
              >
                <SelectTrigger className="bg-background border-border text-foreground">
                  <SelectValue placeholder="Selecione o provedor" />
                </SelectTrigger>
                <SelectContent className="bg-card border-border">
                  <SelectItem value="none" className="text-foreground">
                    Nenhum (Desativado)
                  </SelectItem>
                  <SelectItem value="z-api" className="text-foreground">
                    Z-API
                  </SelectItem>
                  <SelectItem value="uazapi" className="text-foreground">
                    uazapi
                  </SelectItem>
                  <SelectItem value="evolution" className="text-foreground">
                    Evolution API
                  </SelectItem>
                  <SelectItem value="meta-cloud" className="text-foreground">
                    Meta Cloud API
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>

            {/* Aviso de mudança de provedor */}
            {hasExistingIntegration && whatsappProvider !== 'none' && (
              <div className="p-3 bg-primary border-transparent rounded-md">
                <p className="text-primary-foreground text-xs">
                  Para trocar de provedor, primeiro remova a integração atual selecionando "Nenhum"
                  e salvando.
                </p>
              </div>
            )}
          </CardContent>
        </Card>

        {/* Configuração — mostra se um provider está selecionado (z-api OU uazapi). */}
        {/* §7.1: gate por '!== none', credenciais por branch, Buffer/Status/Salvar compartilhados. */}
        {whatsappProvider !== 'none' && !isCreateMode && (
          <>
            {/* Credenciais Z-API — branch z-api (markup INTOCADO) */}
            {whatsappProvider === 'z-api' && (
              <Card className="bg-card border-border">
                <CardHeader>
                  <CardTitle className="text-sm text-foreground">Credenciais Z-API</CardTitle>
                </CardHeader>
                <CardContent className="space-y-4">
                  {/* Telefone */}
                  <div>
                    <Label className="text-muted-foreground">Telefone Conectado *</Label>
                    <Input
                      value={whatsappIdentifier}
                      onChange={(e) => setWhatsappIdentifier(e.target.value)}
                      placeholder="Ex: 5544999999999"
                      className="bg-background border-border text-foreground"
                    />
                    <p className="text-xs text-muted-foreground mt-1">
                      DDI + DDD + Número (sem espaços)
                    </p>
                  </div>

                  {/* Instance ID */}
                  <div>
                    <Label className="text-muted-foreground">Instance ID *</Label>
                    <Input
                      value={whatsappInstanceId}
                      onChange={(e) => setWhatsappInstanceId(e.target.value)}
                      placeholder="ID da instância Z-API"
                      className="bg-background border-border text-foreground"
                    />
                  </div>

                  {/* Token */}
                  <div>
                    <Label className="text-muted-foreground">Token *</Label>
                    <div className="relative">
                      <Input
                        type={showWhatsappToken ? 'text' : 'password'}
                        value={whatsappToken}
                        onChange={(e) => setWhatsappToken(e.target.value)}
                        placeholder="Token da instância"
                        className="bg-background border-border text-foreground pr-10"
                      />
                      <button
                        type="button"
                        onClick={() => setShowWhatsappToken(!showWhatsappToken)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                      >
                        {showWhatsappToken ? (
                          <EyeOff className="w-4 h-4" />
                        ) : (
                          <Eye className="w-4 h-4" />
                        )}
                      </button>
                    </div>
                  </div>

                  {/* Client Token */}
                  <div>
                    <Label className="text-muted-foreground">Client Token *</Label>
                    <div className="relative">
                      <Input
                        type={showWhatsappClientToken ? 'text' : 'password'}
                        value={whatsappClientToken}
                        onChange={(e) => setWhatsappClientToken(e.target.value)}
                        placeholder="Token de segurança adicional"
                        className="bg-background border-border text-foreground pr-10"
                      />
                      <button
                        type="button"
                        onClick={() => setShowWhatsappClientToken(!showWhatsappClientToken)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                      >
                        {showWhatsappClientToken ? (
                          <EyeOff className="w-4 h-4" />
                        ) : (
                          <Eye className="w-4 h-4" />
                        )}
                      </button>
                    </div>
                  </div>

                  {/* Base URL */}
                  <div>
                    <Label className="text-muted-foreground">Base URL</Label>
                    <Input
                      value={whatsappBaseUrl}
                      onChange={(e) => setWhatsappBaseUrl(e.target.value)}
                      placeholder="https://api.z-api.io/instances"
                      className="bg-background border-border text-foreground"
                    />
                    <p className="text-xs text-muted-foreground mt-1">
                      Normalmente não precisa alterar
                    </p>
                  </div>
                </CardContent>
              </Card>
            )}

            {/* Credenciais uazapi — branch uazapi (sem Instance ID / Client Token) */}
            {whatsappProvider === 'uazapi' && (
              <Card className="bg-card border-border">
                <CardHeader>
                  <CardTitle className="text-sm text-foreground">Credenciais uazapi</CardTitle>
                </CardHeader>
                <CardContent className="space-y-4">
                  {/* Telefone conectado -> identifier */}
                  <div>
                    <Label className="text-muted-foreground">Telefone Conectado *</Label>
                    <Input
                      value={whatsappIdentifier}
                      onChange={(e) => setWhatsappIdentifier(e.target.value)}
                      placeholder="Ex: 5544999999999"
                      className="bg-background border-border text-foreground"
                    />
                    <p className="text-xs text-muted-foreground mt-1">
                      DDI + DDD + Número (sem espaços)
                    </p>
                  </div>

                  {/* Host da instância -> base_url */}
                  <div>
                    <Label className="text-muted-foreground">Host da Instância *</Label>
                    <Input
                      value={whatsappBaseUrl}
                      onChange={(e) => setWhatsappBaseUrl(e.target.value)}
                      placeholder="https://sua-instancia.uazapi.com"
                      className="bg-background border-border text-foreground"
                    />
                    <p className="text-xs text-muted-foreground mt-1">
                      URL base da sua instância uazapi
                    </p>
                  </div>

                  {/* Token da instância -> token */}
                  <div>
                    <Label className="text-muted-foreground">Token *</Label>
                    <div className="relative">
                      <Input
                        type={showWhatsappToken ? 'text' : 'password'}
                        value={whatsappToken}
                        onChange={(e) => setWhatsappToken(e.target.value)}
                        placeholder="Token da instância uazapi"
                        className="bg-background border-border text-foreground pr-10"
                      />
                      <button
                        type="button"
                        onClick={() => setShowWhatsappToken(!showWhatsappToken)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                      >
                        {showWhatsappToken ? (
                          <EyeOff className="w-4 h-4" />
                        ) : (
                          <Eye className="w-4 h-4" />
                        )}
                      </button>
                    </div>
                  </div>
                </CardContent>
              </Card>
            )}

            {/* Credenciais Evolution API — branch evolution.
                Backend (route.ts) exige: identifier (Telefone), base_url (Host) e
                instance_id obrigatórios; token = apikey da instância; SEM client_token. */}
            {whatsappProvider === 'evolution' && (
              <Card className="bg-card border-border">
                <CardHeader>
                  <CardTitle className="text-sm text-foreground">
                    Credenciais Evolution API
                  </CardTitle>
                </CardHeader>
                <CardContent className="space-y-4">
                  {/* Telefone conectado -> identifier */}
                  <div>
                    <Label className="text-muted-foreground">Telefone Conectado *</Label>
                    <Input
                      value={whatsappIdentifier}
                      onChange={(e) => setWhatsappIdentifier(e.target.value)}
                      placeholder="Ex: 5544999999999"
                      className="bg-background border-border text-foreground"
                    />
                    <p className="text-xs text-muted-foreground mt-1">
                      DDI + DDD + Número (sem espaços)
                    </p>
                  </div>

                  {/* Host do servidor Evolution -> base_url */}
                  <div>
                    <Label className="text-muted-foreground">Host da Instância *</Label>
                    <Input
                      value={whatsappBaseUrl}
                      onChange={(e) => setWhatsappBaseUrl(e.target.value)}
                      placeholder="https://seu-servidor.evolution-api.com"
                      className="bg-background border-border text-foreground"
                    />
                    <p className="text-xs text-muted-foreground mt-1">
                      URL base do seu servidor Evolution API
                    </p>
                  </div>

                  {/* Nome da instância -> instance_id */}
                  <div>
                    <Label className="text-muted-foreground">Instance ID *</Label>
                    <Input
                      value={whatsappInstanceId}
                      onChange={(e) => setWhatsappInstanceId(e.target.value)}
                      placeholder="Nome da instância no painel Evolution"
                      className="bg-background border-border text-foreground"
                    />
                  </div>

                  {/* API Key da instância -> token */}
                  <div>
                    <Label className="text-muted-foreground">Token (API Key) *</Label>
                    <div className="relative">
                      <Input
                        type={showWhatsappToken ? 'text' : 'password'}
                        value={whatsappToken}
                        onChange={(e) => setWhatsappToken(e.target.value)}
                        placeholder="apikey da instância Evolution"
                        className="bg-background border-border text-foreground pr-10"
                      />
                      <button
                        type="button"
                        onClick={() => setShowWhatsappToken(!showWhatsappToken)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                      >
                        {showWhatsappToken ? (
                          <EyeOff className="w-4 h-4" />
                        ) : (
                          <Eye className="w-4 h-4" />
                        )}
                      </button>
                    </div>
                  </div>
                </CardContent>
              </Card>
            )}

            {/* Credenciais Meta Cloud API — provider oficial WABA. */}
            {whatsappProvider === 'meta-cloud' && (
              <Card className="bg-card border-border">
                <CardHeader>
                  <CardTitle className="text-sm text-foreground">Credenciais Meta Cloud API</CardTitle>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div>
                    <Label className="text-muted-foreground">Telefone Conectado *</Label>
                    <Input
                      value={whatsappIdentifier}
                      onChange={(e) => setWhatsappIdentifier(e.target.value)}
                      placeholder="Ex: 5511999999999"
                      className="bg-background border-border text-foreground"
                    />
                  </div>

                  <div>
                    <Label className="text-muted-foreground">Phone Number ID *</Label>
                    <Input
                      value={whatsappInstanceId}
                      onChange={(e) => setWhatsappInstanceId(e.target.value)}
                      placeholder="ID do número na Meta"
                      className="bg-background border-border text-foreground"
                    />
                  </div>

                  <div>
                    <Label className="text-muted-foreground">WABA ID *</Label>
                    <Input
                      value={whatsappBusinessAccountId}
                      onChange={(e) => setWhatsappBusinessAccountId(e.target.value)}
                      placeholder="WhatsApp Business Account ID"
                      className="bg-background border-border text-foreground"
                    />
                  </div>

                  <div>
                    <Label className="text-muted-foreground">Access Token *</Label>
                    <div className="relative">
                      <Input
                        type={showWhatsappToken ? 'text' : 'password'}
                        value={whatsappToken}
                        onChange={(e) => setWhatsappToken(e.target.value)}
                        placeholder="Token permanente/System User"
                        className="bg-background border-border text-foreground pr-10"
                      />
                      <button
                        type="button"
                        onClick={() => setShowWhatsappToken(!showWhatsappToken)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                      >
                        {showWhatsappToken ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                  </div>

                  <div>
                    <Label className="text-muted-foreground">App Secret *</Label>
                    <div className="relative">
                      <Input
                        type={showWhatsappClientToken ? 'text' : 'password'}
                        value={whatsappClientToken}
                        onChange={(e) => setWhatsappClientToken(e.target.value)}
                        placeholder="App Secret da Meta"
                        className="bg-background border-border text-foreground pr-10"
                      />
                      <button
                        type="button"
                        onClick={() => setShowWhatsappClientToken(!showWhatsappClientToken)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                      >
                        {showWhatsappClientToken ? (
                          <EyeOff className="w-4 h-4" />
                        ) : (
                          <Eye className="w-4 h-4" />
                        )}
                      </button>
                    </div>
                  </div>

                  <div>
                    <Label className="text-muted-foreground">Verify Token *</Label>
                    <Input
                      value={whatsappWebhookVerifyToken}
                      onChange={(e) => setWhatsappWebhookVerifyToken(e.target.value)}
                      placeholder="Token de verificação do webhook Meta"
                      className="bg-background border-border text-foreground"
                    />
                  </div>

                  <div>
                    <Label className="text-muted-foreground">Graph API URL</Label>
                    <Input
                      value={whatsappBaseUrl}
                      onChange={(e) => setWhatsappBaseUrl(e.target.value)}
                      placeholder="https://graph.facebook.com/v23.0"
                      className="bg-background border-border text-foreground"
                    />
                  </div>

                  <div>
                    <Label className="text-muted-foreground">Modo do Webhook</Label>
                    <Select
                      value={whatsappWebhookMode}
                      onValueChange={(value) => setWhatsappWebhookMode(value as 'shadow' | 'active')}
                    >
                      <SelectTrigger className="bg-background border-border text-foreground">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent className="bg-card border-border">
                        <SelectItem value="shadow" className="text-foreground">
                          Shadow
                        </SelectItem>
                        <SelectItem value="active" className="text-foreground">
                          Active
                        </SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </CardContent>
              </Card>
            )}

            {/* URL do Webhook — COMPARTILHADO (provider-agnóstico; só {provider} muda).
                Cutover token-only: exibição read-only + Copiar habilitado (cole no
                painel do provedor). Regenerar disponível, com confirmação avisando
                que a URL antiga deixa de funcionar (SPEC §4.2). */}
            <Card className="bg-card border-border">
              <CardHeader>
                <CardTitle className="text-sm text-foreground">URL do Webhook</CardTitle>
                <p className="text-xs text-muted-foreground">
                  Cole esta URL no campo de webhook do painel do seu provedor para receber
                  mensagens.
                </p>
              </CardHeader>
              <CardContent className="space-y-4">
                {webhookUrl ? (
                  <>
                    <div className="flex items-center gap-2">
                      <Input
                        readOnly
                        value={webhookUrl}
                        className="bg-background border-border text-foreground font-mono text-xs"
                      />
                      <Button
                        type="button"
                        variant="outline"
                        size="icon"
                        title="Copiar URL do webhook"
                        className="shrink-0"
                        onClick={() => {
                          navigator.clipboard.writeText(webhookUrl).then(() => {
                            setCopiedWebhookUrl(true);
                            setTimeout(() => setCopiedWebhookUrl(false), 2000);
                          });
                        }}
                      >
                        {copiedWebhookUrl ? (
                          <Check className="h-4 w-4 text-success" />
                        ) : (
                          <Copy className="h-4 w-4" />
                        )}
                      </Button>
                    </div>
                    <p className="text-xs text-muted-foreground">
                      Cole esta URL no campo de webhook do painel do seu provedor.
                    </p>
                  </>
                ) : (
                  <p className="text-xs text-muted-foreground">
                    {whatsappWebhookToken
                      ? 'URL indisponível: configure NEXT_PUBLIC_API_URL (domínio público https) no backend.'
                      : 'A URL do webhook é gerada ao salvar a integração.'}
                  </p>
                )}

                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setConfirmRegenerate(true)}
                  disabled={regeneratingWebhook || !whatsappWebhookToken}
                  className="w-full"
                >
                  {regeneratingWebhook ? (
                    <>
                      <Loader2 className="h-4 w-4 animate-spin mr-2" />
                      Regenerando...
                    </>
                  ) : (
                    <>
                      <RefreshCw className="h-4 w-4 mr-2" />
                      Regenerar token
                    </>
                  )}
                </Button>
              </CardContent>
            </Card>

            {/* Buffer Settings — COMPARTILHADO (fora do branch de provider) */}
            <Card className="bg-card border-border">
              <CardHeader>
                <CardTitle className="text-sm text-foreground">Buffer de Mensagens</CardTitle>
                <p className="text-xs text-muted-foreground">
                  Agrupa mensagens rápidas consecutivas antes de processar
                </p>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="flex items-center justify-between">
                  <div>
                    <Label className="text-muted-foreground">Habilitar Buffer</Label>
                    <p className="text-xs text-muted-foreground">Reduz chamadas de LLM em ~80%</p>
                  </div>
                  <Switch
                    checked={whatsappBufferEnabled}
                    onCheckedChange={setWhatsappBufferEnabled}
                  />
                </div>

                {whatsappBufferEnabled && (
                  <>
                    <div>
                      <Label className="text-muted-foreground">Debounce (segundos)</Label>
                      <Input
                        type="number"
                        min={1}
                        max={30}
                        value={whatsappBufferDebounce}
                        onChange={(e) => setWhatsappBufferDebounce(parseInt(e.target.value) || 3)}
                        className="bg-background border-border text-foreground"
                      />
                      <p className="text-xs text-muted-foreground mt-1">
                        Aguarda X segundos após última mensagem (recomendado: 3)
                      </p>
                    </div>

                    <div>
                      <Label className="text-muted-foreground">Max Wait (segundos)</Label>
                      <Input
                        type="number"
                        min={5}
                        max={60}
                        value={whatsappBufferMaxWait}
                        onChange={(e) => setWhatsappBufferMaxWait(parseInt(e.target.value) || 10)}
                        className="bg-background border-border text-foreground"
                      />
                      <p className="text-xs text-muted-foreground mt-1">
                        Tempo máximo desde primeira mensagem (recomendado: 10)
                      </p>
                    </div>
                  </>
                )}
              </CardContent>
            </Card>

            {/* Status — COMPARTILHADO */}
            <Card className="bg-card border-border">
              <CardContent className="pt-4">
                <div className="flex items-center justify-between">
                  <div>
                    <Label className="text-muted-foreground">Integração Ativa</Label>
                    <p className="text-xs text-muted-foreground">
                      Habilita recebimento de mensagens
                    </p>
                  </div>
                  <Switch checked={whatsappIsActive} onCheckedChange={setWhatsappIsActive} />
                </div>
              </CardContent>
            </Card>

            {/* Save Button — COMPARTILHADO (z-api E uazapi) */}
            <Button
              onClick={onSaveWhatsapp}
              disabled={savingWhatsapp}
              className="w-full bg-success hover:bg-success/90 text-primary-foreground"
            >
              {savingWhatsapp ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin mr-2" />
                  Salvando WhatsApp...
                </>
              ) : (
                <>
                  <MessageCircle className="h-4 w-4 mr-2" />
                  Salvar Configuração WhatsApp
                </>
              )}
            </Button>
          </>
        )}
      </div>

      {/* Confirmação de regeneração: a URL antiga para de funcionar na hora. */}
      <ConfirmDialog
        open={confirmRegenerate}
        onOpenChange={setConfirmRegenerate}
        title="Regenerar token do webhook?"
        description="A URL atual deixará de funcionar imediatamente. Você precisará copiar a nova URL e colá-la novamente no painel do seu provedor para continuar recebendo mensagens."
        confirmLabel="Regenerar"
        destructive
        onConfirm={() => {
          setConfirmRegenerate(false);
          onRegenerateWebhookToken();
        }}
      />
    </>
  );
}
