'use client';

import { Badge } from '@/components/ui/badge';
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
import { Slider } from '@/components/ui/slider';
import { Switch } from '@/components/ui/switch';
import { AlertTriangle, Brain, CheckCircle, Loader2, TestTube } from 'lucide-react';

// =============================================================================
// Catálogo de modelos — carregado dinamicamente de GET /catalog
// =============================================================================

export interface ProviderInfo {
  name: string;
  display_name: string;
  models_count: number;
}

export interface ModelCapabilities {
  temperature: boolean;
  reasoning_effort: boolean;
  thinking: boolean;
  thinking_api: null | 'anthropic' | 'level' | 'budget';
  vision: boolean;
  tools: boolean;
  verbosity: boolean;
}

export interface CatalogEntry {
  model_id: string;
  provider: string;
  label: string;
  tier: string | null;
  recommended: boolean;
  selectable: boolean;
  capabilities: ModelCapabilities;
  pricing: {
    input_per_million: number | null;
    output_per_million: number | null;
    unit: string;
  };
}

interface Props {
  providers: ProviderInfo[];
  modelOptions: CatalogEntry[];
  selectedCaps: ModelCapabilities | undefined;
  llmProvider: string | undefined;
  setLlmProvider: (value: string | undefined) => void;
  llmModel: string | undefined;
  setLlmModel: (value: string | undefined) => void;
  modelSearch: string;
  setModelSearch: (value: string) => void;
  temperature: number;
  setTemperature: (value: number) => void;
  maxTokens: number;
  setMaxTokens: (value: number) => void;
  topP: number;
  setTopP: (value: number) => void;
  topK: number;
  setTopK: (value: number) => void;
  frequencyPenalty: number;
  setFrequencyPenalty: (value: number) => void;
  presencePenalty: number;
  setPresencePenalty: (value: number) => void;
  reasoningEffort: string;
  setReasoningEffort: (value: string) => void;
  verbosity: string;
  setVerbosity: (value: string) => void;
  thinkingEnabled: boolean;
  setThinkingEnabled: (value: boolean) => void;
  testingLLM: boolean;
  testResult: { status: 'success' | 'error'; message: string } | null;
  onTestLLM: () => void;
}

export function ModelSection({
  providers,
  modelOptions,
  selectedCaps,
  llmProvider,
  setLlmProvider,
  llmModel,
  setLlmModel,
  modelSearch,
  setModelSearch,
  temperature,
  setTemperature,
  maxTokens,
  setMaxTokens,
  topP,
  setTopP,
  topK,
  setTopK,
  frequencyPenalty,
  setFrequencyPenalty,
  presencePenalty,
  setPresencePenalty,
  reasoningEffort,
  setReasoningEffort,
  verbosity,
  setVerbosity,
  thinkingEnabled,
  setThinkingEnabled,
  testingLLM,
  testResult,
  onTestLLM,
}: Props) {
  return (
    <div className="space-y-6">
      <Card className="bg-card border-border">
        <CardHeader>
          <CardTitle className="text-sm text-foreground">Modelo de IA</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div>
            <Label htmlFor="provider" className="text-muted-foreground">
              Provider
            </Label>
            <Select
              value={llmProvider}
              onValueChange={(value) => {
                setLlmProvider(value);
                setLlmModel(undefined);
              }}
            >
              <SelectTrigger className="bg-background border-border text-foreground">
                <SelectValue placeholder="Selecione o provider" />
              </SelectTrigger>
              <SelectContent className="bg-card border-border">
                {providers.map((p) => (
                  <SelectItem key={p.name} value={p.name} className="text-foreground">
                    {p.display_name} ({p.models_count} modelos)
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div>
            <Label htmlFor="model" className="text-muted-foreground">
              Modelo
            </Label>
            <Select
              value={llmModel}
              onValueChange={(model) => {
                setLlmModel(model);
                // Forçar temperatura 1.0 quando o modelo não suporta temperatura customizada
                const entry = modelOptions.find((m) => m.model_id === model);
                if (entry?.capabilities.temperature === false) {
                  setTemperature(1.0);
                }
                // Limpar controles avançados que o novo modelo não suporta
                if (!entry?.capabilities.reasoning_effort) setReasoningEffort('none');
                else if (reasoningEffort === 'none') setReasoningEffort('medium');
                if (!entry?.capabilities.thinking) setThinkingEnabled(false);
              }}
              disabled={!llmProvider}
            >
              <SelectTrigger className="bg-background border-border text-foreground">
                <SelectValue placeholder="Selecione o modelo" />
              </SelectTrigger>
              <SelectContent
                className="bg-card border-border max-h-[40vh] overflow-y-auto z-[9999] min-w-[300px] w-[var(--radix-select-trigger-width)]"
                position="popper"
                sideOffset={5}
              >
                {llmProvider === 'openrouter' && (
                  <div className="sticky top-0 bg-card p-2 border-b border-border z-10">
                    <input
                      type="text"
                      placeholder="Buscar modelo... (ex: llama, deepseek, mistral)"
                      value={modelSearch}
                      onChange={(e) => setModelSearch(e.target.value)}
                      className="w-full px-2 py-1 text-sm bg-background border border-border rounded text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary"
                      onClick={(e) => e.stopPropagation()}
                      onKeyDown={(e) => e.stopPropagation()}
                    />
                  </div>
                )}
                {modelOptions
                  .filter(
                    (opt) =>
                      !modelSearch ||
                      opt.label.toLowerCase().includes(modelSearch.toLowerCase()) ||
                      opt.model_id.toLowerCase().includes(modelSearch.toLowerCase()),
                  )
                  .map((opt) => (
                    <SelectItem
                      key={opt.model_id}
                      value={opt.model_id}
                      className="text-foreground truncate max-w-[500px] cursor-pointer focus:bg-primary focus:text-primary-foreground"
                    >
                      <span className="flex items-center gap-2">
                        <span className="truncate">{opt.label}</span>
                        {opt.tier && (
                          <Badge
                            variant="outline"
                            className="text-[10px] px-1 py-0 border-primary/20 text-primary"
                          >
                            {opt.tier}
                          </Badge>
                        )}
                        {opt.recommended && (
                          <Badge
                            variant="outline"
                            className="text-[10px] px-1 py-0 border-success/20 text-success"
                          >
                            Recomendado
                          </Badge>
                        )}
                      </span>
                    </SelectItem>
                  ))}
              </SelectContent>
            </Select>
          </div>

          {/* Test LLM Integration Button */}
          <div className="flex items-center gap-3 pt-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={onTestLLM}
              disabled={testingLLM || !llmProvider || !llmModel}
              className="bg-background border-border text-foreground hover:bg-muted"
            >
              {testingLLM ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Testando...
                </>
              ) : (
                <>
                  <TestTube className="mr-2 h-4 w-4" />
                  Testar Integração
                </>
              )}
            </Button>
            {testResult && (
              <div
                className={`flex items-center gap-2 text-sm ${testResult.status === 'success' ? 'text-success' : 'text-danger'}`}
              >
                {testResult.status === 'success' ? (
                  <CheckCircle className="h-4 w-4" />
                ) : (
                  <AlertTriangle className="h-4 w-4" />
                )}
                <span className="truncate max-w-[200px]">{testResult.message}</span>
              </div>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Parameters */}
      <Card className="bg-card border-border">
        <CardHeader>
          <CardTitle className="text-sm text-foreground">Parâmetros</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div>
            <div className="flex justify-between mb-2">
              <Label className="text-muted-foreground">Temperature</Label>
              <span className="text-sm text-muted-foreground">
                {selectedCaps?.temperature === false ? '1.00 (fixo)' : temperature.toFixed(2)}
              </span>
            </div>
            <Slider
              value={[selectedCaps?.temperature === false ? 1.0 : temperature]}
              onValueChange={(value) =>
                selectedCaps?.temperature !== false && setTemperature(value[0])
              }
              min={0}
              max={2}
              step={0.1}
              className="w-full"
              disabled={selectedCaps?.temperature === false}
            />
            <p className="text-xs text-muted-foreground mt-1">
              {selectedCaps?.temperature === false
                ? 'Este modelo só suporta temperature 1.0'
                : 'Menor = mais conservador, Maior = mais criativo'}
            </p>
          </div>

          <div>
            <Label htmlFor="max_tokens" className="text-muted-foreground">
              Max Tokens
            </Label>
            <Input
              id="max_tokens"
              type="number"
              value={maxTokens}
              onChange={(e) => setMaxTokens(parseInt(e.target.value))}
              min={100}
              max={100000}
              className="bg-background border-border text-foreground"
            />
          </div>
        </CardContent>
      </Card>

      {/* Advanced Parameters */}
      <Card className="bg-card border-border">
        <CardHeader>
          <CardTitle className="text-sm text-foreground">Parâmetros Avançados</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div>
            <div className="flex justify-between mb-2">
              <Label className="text-muted-foreground">Top P</Label>
              <span className="text-sm text-muted-foreground">{topP.toFixed(2)}</span>
            </div>
            <Slider
              value={[topP]}
              onValueChange={(value) => setTopP(value[0])}
              min={0}
              max={1}
              step={0.01}
              className="w-full"
            />
          </div>

          <div>
            <Label htmlFor="top_k" className="text-muted-foreground">
              Top K
            </Label>
            <Input
              id="top_k"
              type="number"
              value={topK}
              onChange={(e) => setTopK(parseInt(e.target.value))}
              min={1}
              max={100}
              className="bg-background border-border text-foreground"
            />
          </div>

          <div>
            <div className="flex justify-between mb-2">
              <Label className="text-muted-foreground">Frequency Penalty</Label>
              <span className="text-sm text-muted-foreground">{frequencyPenalty.toFixed(2)}</span>
            </div>
            <Slider
              value={[frequencyPenalty]}
              onValueChange={(value) => setFrequencyPenalty(value[0])}
              min={-2}
              max={2}
              step={0.1}
              className="w-full"
            />
          </div>

          <div>
            <div className="flex justify-between mb-2">
              <Label className="text-muted-foreground">Presence Penalty</Label>
              <span className="text-sm text-muted-foreground">{presencePenalty.toFixed(2)}</span>
            </div>
            <Slider
              value={[presencePenalty]}
              onValueChange={(value) => setPresencePenalty(value[0])}
              min={-2}
              max={2}
              step={0.1}
              className="w-full"
            />
          </div>
        </CardContent>
      </Card>

      {/* Reasoning / Thinking / Verbosity — dinâmico por capability */}
      {(selectedCaps?.reasoning_effort || selectedCaps?.thinking || selectedCaps?.verbosity) && (
        <Card className="bg-card border-border border-accent/25">
          <CardHeader>
            <CardTitle className="text-sm text-foreground flex items-center gap-2">
              <Brain className="h-4 w-4 text-accent" />
              Configurações de Raciocínio
            </CardTitle>
            <p className="text-xs text-muted-foreground">
              Controles avançados disponíveis para este modelo
            </p>
          </CardHeader>
          <CardContent className="space-y-4">
            {selectedCaps?.reasoning_effort && (
              <div>
                <Label className="text-muted-foreground">Reasoning Effort</Label>
                <Select value={reasoningEffort} onValueChange={setReasoningEffort}>
                  <SelectTrigger className="bg-background border-border text-foreground">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent className="bg-card border-border">
                    <SelectItem value="low" className="text-foreground">
                      Low (Raciocínio leve)
                    </SelectItem>
                    <SelectItem value="medium" className="text-foreground">
                      Medium (Balanceado)
                    </SelectItem>
                    <SelectItem value="high" className="text-foreground">
                      High (Raciocínio profundo)
                    </SelectItem>
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground mt-1">
                  Controla a profundidade do raciocínio. Valores mais altos = respostas melhores,
                  porém mais tokens.
                </p>
              </div>
            )}

            {selectedCaps?.thinking && (
              <div className="flex items-center justify-between">
                <div>
                  <Label className="text-muted-foreground">Pensamento estendido (thinking)</Label>
                  <p className="text-xs text-muted-foreground mt-1">
                    Permite que o modelo raciocine antes de responder.
                  </p>
                </div>
                <Switch checked={thinkingEnabled} onCheckedChange={setThinkingEnabled} />
              </div>
            )}

            {selectedCaps?.verbosity && (
              <div>
                <Label className="text-muted-foreground">Verbosity</Label>
                <Select value={verbosity} onValueChange={setVerbosity}>
                  <SelectTrigger className="bg-background border-border text-foreground">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent className="bg-card border-border">
                    <SelectItem value="low" className="text-foreground">
                      Low (Respostas concisas)
                    </SelectItem>
                    <SelectItem value="medium" className="text-foreground">
                      Medium (Balanceado)
                    </SelectItem>
                    <SelectItem value="high" className="text-foreground">
                      High (Respostas detalhadas)
                    </SelectItem>
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground mt-1">
                  Controla o nível de detalhamento das respostas.
                </p>
              </div>
            )}

            {selectedCaps?.temperature === false && (
              <div className="pt-2 border-t border-border">
                <p className="text-xs text-warning">
                  Modelos de raciocínio ignoram o parâmetro Temperature.
                </p>
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
