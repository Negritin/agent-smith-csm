'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { AlertTriangle, Eye, FileCode, Globe, Lock, Shield } from 'lucide-react';

// =============================================================================
// DEFAULT BLACKLIST (Sync with backend)
// =============================================================================
const DEFAULT_BLACKLIST = [
  'bit.ly',
  'tinyurl.com',
  't.co',
  'goo.gl',
  'shorturl.at',
  'rb.gy',
  'is.gd',
  'owl.li',
  'malware.com',
  'phishing.org',
].join('\n');

interface Props {
  securityEnabled: boolean;
  setSecurityEnabled: (value: boolean) => void;
  checkJailbreak: boolean;
  setCheckJailbreak: (value: boolean) => void;
  piiAction: string;
  setPiiAction: (value: string) => void;
  checkSecretKeys: boolean;
  setCheckSecretKeys: (value: boolean) => void;
  failClose: boolean;
  setFailClose: (value: boolean) => void;
  urlMode: string;
  setUrlMode: (value: string) => void;
  urlWhitelist: string;
  setUrlWhitelist: (value: string) => void;
  urlBlacklist: string;
  setUrlBlacklist: (value: string) => void;
  allowedTopics: string;
  setAllowedTopics: (value: string) => void;
  customRegex: string;
  setCustomRegex: (value: string) => void;
  securityErrorMessage: string;
  setSecurityErrorMessage: (value: string) => void;
}

export function SecuritySection({
  securityEnabled,
  setSecurityEnabled,
  checkJailbreak,
  setCheckJailbreak,
  piiAction,
  setPiiAction,
  checkSecretKeys,
  setCheckSecretKeys,
  failClose,
  setFailClose,
  urlMode,
  setUrlMode,
  urlWhitelist,
  setUrlWhitelist,
  urlBlacklist,
  setUrlBlacklist,
  allowedTopics,
  setAllowedTopics,
  customRegex,
  setCustomRegex,
  securityErrorMessage,
  setSecurityErrorMessage,
}: Props) {
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between bg-danger/10 p-4 rounded-lg border border-danger/25">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <Shield className="w-5 h-5 text-danger" />
            <Label className="text-base font-semibold text-foreground">
              Ativar Guardrails de Segurança
            </Label>
          </div>
          <p className="text-sm text-muted-foreground">
            Habilita a camada de proteção para prevenir respostas tóxicas e vazamento de dados.
          </p>
        </div>
        <Switch checked={securityEnabled} onCheckedChange={setSecurityEnabled} />
      </div>

      {securityEnabled && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6 animate-in fade-in slide-in-from-top-2">
          {/* Coluna 1: AI Safety & PII */}
          <div className="space-y-6">
            <Card className="bg-card border-border">
              <CardHeader className="pb-3">
                <CardTitle className="text-sm text-foreground flex items-center gap-2">
                  <Eye className="w-4 h-4 text-primary" /> AI Safety & Conteúdo
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="flex items-center justify-between">
                  <div className="space-y-0.5">
                    <Label className="text-foreground">Detectar Jailbreak</Label>
                    <p className="text-xs text-muted-foreground">
                      Bloqueia prompt injection e jailbreak via Prompt Guard 2 (Groq). Faz uma
                      verificação externa por mensagem — ligue só em agentes expostos a entradas não
                      confiáveis.
                    </p>
                  </div>
                  <Switch checked={checkJailbreak} onCheckedChange={setCheckJailbreak} />
                </div>
              </CardContent>
            </Card>

            <Card className="bg-card border-border">
              <CardHeader className="pb-3">
                <CardTitle className="text-sm text-foreground flex items-center gap-2">
                  <Lock className="w-4 h-4 text-warning" /> Dados Pessoais (PII)
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="space-y-4">
                  <div className="flex items-center justify-between">
                    <div className="space-y-0.5">
                      <Label className="text-foreground">Proteger Dados Pessoais</Label>
                      <p className="text-xs text-muted-foreground">
                        Detecta e protege CPF, Email, Telefone, etc.
                      </p>
                    </div>
                    <Switch
                      checked={piiAction !== 'off'}
                      onCheckedChange={(checked) => {
                        setPiiAction(checked ? 'mask' : 'off');
                      }}
                    />
                  </div>

                  {piiAction !== 'off' && (
                    <div className="space-y-2 animate-in fade-in slide-in-from-top-1">
                      <Label className="text-xs text-muted-foreground">Ação ao detectar</Label>
                      <Select value={piiAction} onValueChange={setPiiAction}>
                        <SelectTrigger className="bg-background border-border text-foreground h-8 text-xs">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent className="bg-card border-border text-foreground">
                          <SelectItem value="mask">Mascarar (Substituir por asteriscos)</SelectItem>
                          <SelectItem value="block">Bloquear Mensagem</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                  )}
                </div>
                <div className="flex items-center justify-between pt-2">
                  <div className="space-y-0.5">
                    <Label className="text-foreground">Bloquear Secret Keys</Label>
                    <p className="text-xs text-muted-foreground">
                      Detecta e bloqueia chaves de API (sk-..., gh_...)
                    </p>
                  </div>
                  <Switch checked={checkSecretKeys} onCheckedChange={setCheckSecretKeys} />
                </div>

                <div className="flex items-center justify-between pt-2">
                  <div className="space-y-0.5">
                    <Label className="text-foreground">Bloquear se IA Falhar (Fail-Close)</Label>
                    <p className="text-xs text-muted-foreground">
                      Se a API de segurança cair, bloqueia a mensagem por precaução.
                    </p>
                  </div>
                  <Switch checked={failClose} onCheckedChange={setFailClose} />
                </div>
              </CardContent>
            </Card>
          </div>

          {/* Coluna 2: Regras e Customização */}
          <div className="space-y-6">
            <Card className="bg-card border-border">
              <CardHeader className="pb-3">
                <CardTitle className="text-sm text-foreground flex items-center gap-2">
                  <Globe className="w-4 h-4 text-success" /> URLs e Tópicos
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="flex items-center justify-between">
                  <div className="space-y-0.5">
                    <Label className="text-foreground">Proteção de URLs</Label>
                    <p className="text-xs text-muted-foreground">
                      Controle quais links podem ser processados
                    </p>
                  </div>
                </div>

                <RadioGroup
                  value={urlMode}
                  onValueChange={(val) => {
                    setUrlMode(val);
                    // Auto-fill default blacklist if empty when switching to blacklist
                    if (val === 'blacklist' && !urlBlacklist.trim()) {
                      setUrlBlacklist(DEFAULT_BLACKLIST);
                    }
                  }}
                  className="grid grid-cols-1 md:grid-cols-3 gap-4 pt-2"
                >
                  <div>
                    <RadioGroupItem value="off" id="url-off" className="peer sr-only" />
                    <Label
                      htmlFor="url-off"
                      className="flex flex-col items-center justify-between rounded-md border-2 border-border bg-card p-4 hover:bg-muted hover:text-foreground peer-data-[state=checked]:border-primary [&:has([data-state=checked])]:border-primary cursor-pointer"
                    >
                      <Globe className="mb-3 h-6 w-6 text-muted-foreground peer-data-[state=checked]:text-primary" />
                      <span className="text-xs text-muted-foreground">Desativado</span>
                    </Label>
                  </div>

                  <div>
                    <RadioGroupItem value="whitelist" id="url-white" className="peer sr-only" />
                    <Label
                      htmlFor="url-white"
                      className="flex flex-col items-center justify-between rounded-md border-2 border-border bg-card p-4 hover:bg-muted hover:text-foreground peer-data-[state=checked]:border-primary [&:has([data-state=checked])]:border-primary cursor-pointer"
                    >
                      <Shield className="mb-3 h-6 w-6 text-muted-foreground peer-data-[state=checked]:text-success" />
                      <span className="text-xs text-muted-foreground">Whitelist</span>
                    </Label>
                  </div>

                  <div>
                    <RadioGroupItem value="blacklist" id="url-black" className="peer sr-only" />
                    <Label
                      htmlFor="url-black"
                      className="flex flex-col items-center justify-between rounded-md border-2 border-border bg-card p-4 hover:bg-muted hover:text-foreground peer-data-[state=checked]:border-primary [&:has([data-state=checked])]:border-primary cursor-pointer"
                    >
                      <Lock className="mb-3 h-6 w-6 text-muted-foreground peer-data-[state=checked]:text-danger" />
                      <span className="text-xs text-muted-foreground">Blacklist</span>
                    </Label>
                  </div>
                </RadioGroup>

                {urlMode === 'whitelist' && (
                  <div className="space-y-2 pt-2 animate-in fade-in slide-in-from-top-2">
                    <Label className="text-xs text-muted-foreground">
                      Domínios Permitidos (Whitelist)
                    </Label>
                    <Textarea
                      value={urlWhitelist}
                      onChange={(e) => setUrlWhitelist(e.target.value)}
                      placeholder="google.com&#10;openai.com&#10;*.empresa.com.br"
                      className="bg-background border-border text-foreground text-xs font-mono h-24"
                    />
                    <p className="text-[10px] text-muted-foreground">
                      Apenas URLs destes domínios serão permitidas. Suporta wildcards.
                    </p>
                  </div>
                )}

                {urlMode === 'blacklist' && (
                  <div className="space-y-2 pt-2 animate-in fade-in slide-in-from-top-2">
                    <Label className="text-xs text-muted-foreground">
                      Domínios Bloqueados (Blacklist)
                    </Label>
                    <Textarea
                      value={urlBlacklist}
                      onChange={(e) => setUrlBlacklist(e.target.value)}
                      placeholder="bit.ly&#10;malware.com&#10;*.badsite.org"
                      className="bg-background border-border text-foreground text-xs font-mono h-24"
                    />
                    <p className="text-[10px] text-muted-foreground">
                      Estas URLs serão bloqueadas. Já inclui encurtadores comuns por padrão.
                    </p>
                  </div>
                )}

                <div className="space-y-2 pt-2">
                  <Label className="text-foreground">Tópicos Permitidos (Opcional)</Label>
                  <Textarea
                    value={allowedTopics}
                    onChange={(e) => setAllowedTopics(e.target.value)}
                    placeholder="Vendas, Suporte Técnico, Preços..."
                    className="bg-background border-border text-foreground text-xs h-20"
                  />
                  <p className="text-[10px] text-muted-foreground">
                    Se preenchido, o agente recusará falar sobre outros assuntos (Topical
                    Alignment).
                  </p>
                </div>
              </CardContent>
            </Card>

            <Card className="bg-card border-border">
              <CardHeader className="pb-3">
                <CardTitle className="text-sm text-foreground flex items-center gap-2">
                  <AlertTriangle className="w-4 h-4 text-warning" /> Resposta de Bloqueio
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="space-y-2">
                  <Label className="text-foreground">Mensagem ao Usuário</Label>
                  <Input
                    value={securityErrorMessage}
                    onChange={(e) => setSecurityErrorMessage(e.target.value)}
                    className="bg-background border-border text-foreground"
                  />
                  <p className="text-xs text-muted-foreground">
                    Esta mensagem será enviada quando o guardrail bloquear o input, sem expor o
                    motivo técnico.
                  </p>
                </div>

                <div className="space-y-2 pt-2">
                  <Label className="text-foreground flex items-center gap-1">
                    <FileCode className="w-3 h-3" /> Regex Customizado (Avançado)
                  </Label>
                  <Textarea
                    value={customRegex}
                    onChange={(e) => setCustomRegex(e.target.value)}
                    placeholder="^.*(concorrente|palavra_proibida).*$"
                    className="bg-background border-border text-foreground text-xs font-mono h-16"
                  />
                </div>
              </CardContent>
            </Card>
          </div>
        </div>
      )}
    </div>
  );
}
