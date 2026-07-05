'use client';

import { useState, useEffect, useRef, useMemo } from 'react';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import {
  ArrowLeft,
  Brain,
  CheckCircle,
  Code,
  Database,
  Loader2,
  MessageCircle,
  Plug,
  Settings,
  Shield,
  ShoppingCart,
  Sparkles,
  Terminal,
  Timer,
  Users,
} from 'lucide-react';
import { useToast } from '@/hooks/use-toast';
import { HttpTool } from '@/components/admin/HttpToolForm';
import { Agent, WidgetConfig, SecuritySettings } from '@/types/agent';
import { mergeGeneralSaveToolsConfig } from '@/lib/tools-config-merge';
import { IdentitySection } from './sections/IdentitySection';
import { SecuritySection } from './sections/SecuritySection';
import {
  ModelSection,
  ProviderInfo,
  CatalogEntry,
  ModelCapabilities,
} from './sections/ModelSection';
import {
  PersonalitySection,
  DEFAULT_SYSTEM_PROMPT,
  ContextVariable,
} from './sections/PersonalitySection';
import { MemorySection } from './sections/MemorySection';
import { AttendanceSection } from './sections/AttendanceSection';
import { ToolsSection } from './sections/ToolsSection';
import { MCPSection } from './sections/MCPSection';
import { CommerceSection } from './sections/CommerceSection';
import { WidgetSection } from './sections/WidgetSection';
import { WhatsAppSection } from './sections/WhatsAppSection';
import { SubagentsSection } from './sections/SubagentsSection';

export type AgentConfigSectionId =
  | 'identity'
  | 'model'
  | 'personality'
  | 'attendance'
  | 'memory'
  | 'security'
  | 'tools'
  | 'mcp'
  | 'commerce'
  | 'widget'
  | 'whatsapp'
  | 'subagents';

const SECTION_IDS: AgentConfigSectionId[] = [
  'identity',
  'model',
  'personality',
  'attendance',
  'memory',
  'security',
  'tools',
  'mcp',
  'commerce',
  'widget',
  'whatsapp',
  'subagents',
];

interface Props {
  companyId: string;
  agentId?: string; // Optional: undefined = create mode
  onBack: () => void;
  onSaved?: (agentId: string) => void; // chamado após criação com sucesso
  initialSection?: string; // deep-link ?section=
}

export function AgentConfigView({ companyId, agentId, onBack, onSaved, initialSection }: Props) {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [models, setModels] = useState<CatalogEntry[]>([]);

  // Agent Identity
  const [name, setName] = useState('');
  const [slug, setSlug] = useState('');
  const [avatarUrl, setAvatarUrl] = useState('');

  // LLM Config
  const [llmProvider, setLlmProvider] = useState<string | undefined>(undefined);
  const [llmModel, setLlmModel] = useState<string | undefined>(undefined);
  const [temperature, setTemperature] = useState(0.7);
  const [maxTokens, setMaxTokens] = useState(2000);
  const [topP, setTopP] = useState(1.0);
  const [topK, setTopK] = useState(40);
  const [frequencyPenalty, setFrequencyPenalty] = useState(0.0);
  const [presencePenalty, setPresencePenalty] = useState(0.0);

  // LLM Advanced Config (GPT-5.x, o1, o3)
  const [reasoningEffort, setReasoningEffort] = useState<string>('medium');
  const [verbosity, setVerbosity] = useState<string>('medium');
  const [thinkingEnabled, setThinkingEnabled] = useState(false);

  // Behavior
  const [systemPrompt, setSystemPrompt] = useState('');
  const [allowWebSearch, setAllowWebSearch] = useState(true);
  const [allowVision, setAllowVision] = useState(false);
  const [visionModel, setVisionModel] = useState<string | undefined>(undefined);
  const [isHydeEnabled, setIsHydeEnabled] = useState(true); // HyDE toggle
  const [modelSearch, setModelSearch] = useState(''); // OpenRouter model search filter

  // Security - Guardrails
  const [securityEnabled, setSecurityEnabled] = useState(false);
  const [checkJailbreak, setCheckJailbreak] = useState(true);
  const [checkNsfw, setCheckNsfw] = useState(true);
  const [piiAction, setPiiAction] = useState('mask'); // mask, block, off
  const [checkSecretKeys, setCheckSecretKeys] = useState(true);
  const [checkUrls, setCheckUrls] = useState(false);

  // URL Protection
  const [urlMode, setUrlMode] = useState<string>('off');
  const [urlBlacklist, setUrlBlacklist] = useState('');
  const [urlWhitelist, setUrlWhitelist] = useState('');
  const [allowedTopics, setAllowedTopics] = useState('');
  const [customRegex, setCustomRegex] = useState('');
  const [failClose, setFailClose] = useState(true);
  const [securityErrorMessage, setSecurityErrorMessage] = useState(
    'Sua mensagem viola as políticas de segurança.',
  );

  // WhatsApp Integration - POR AGENTE
  const ZAPI_DEFAULT_BASE_URL = 'https://api.z-api.io/instances';
  const [whatsappProvider, setWhatsappProvider] = useState<string>('none');
  const [whatsappIdentifier, setWhatsappIdentifier] = useState('');
  const [whatsappInstanceId, setWhatsappInstanceId] = useState('');
  const [whatsappToken, setWhatsappToken] = useState('');
  const [whatsappClientToken, setWhatsappClientToken] = useState('');
  const [whatsappBaseUrl, setWhatsappBaseUrl] = useState(ZAPI_DEFAULT_BASE_URL);
  const [whatsappIsActive, setWhatsappIsActive] = useState(true);
  const [whatsappBufferEnabled, setWhatsappBufferEnabled] = useState(true);
  const [whatsappBufferDebounce, setWhatsappBufferDebounce] = useState(3);
  const [whatsappBufferMaxWait, setWhatsappBufferMaxWait] = useState(10);
  const [whatsappBusinessAccountId, setWhatsappBusinessAccountId] = useState('');
  const [whatsappWebhookVerifyToken, setWhatsappWebhookVerifyToken] = useState('');
  const [whatsappWebhookMode, setWhatsappWebhookMode] = useState<'shadow' | 'active'>('shadow');
  const [hasExistingIntegration, setHasExistingIntegration] = useState(false);
  const [integrationId, setIntegrationId] = useState<string | null>(null);
  const [savingWhatsapp, setSavingWhatsapp] = useState(false);
  // Token de webhook por-integração (Fase 1): carregados do GET, montados
  // server-side a partir de NEXT_PUBLIC_API_URL (sem fallback localhost). Ver SPEC §4.3.
  const [whatsappWebhookToken, setWhatsappWebhookToken] = useState('');
  const [webhookUrlBase, setWebhookUrlBase] = useState('');
  const [regeneratingWebhook, setRegeneratingWebhook] = useState(false);

  // Human Handoff & Tools
  const [allowHumanHandoff, setAllowHumanHandoff] = useState(false);
  const [allowCsvAnalytics, setAllowCsvAnalytics] = useState(false);
  // tools_config carregado do agente, preservado para deep-merge no save (S5).
  // A UI só edita human_handoff/csv_analytics; chaves não editadas aqui
  // (end_attendance, e quaisquer chaves futuras/desconhecidas) NÃO podem ser
  // apagadas por um save não relacionado (ex.: trocar prompt/modelo).
  const loadedToolsConfigRef = useRef<Record<string, unknown>>({});

  // SubAgent Config
  const [isSubagent, setIsSubagent] = useState(false);
  const [allowDirectChat, setAllowDirectChat] = useState(false);

  // Widget Config
  const [widgetConfig, setWidgetConfig] = useState<WidgetConfig>({});

  // HTTP Tools Management
  const [toolsView, setToolsView] = useState<'list' | 'form'>('list');
  const [httpTools, setHttpTools] = useState<HttpTool[]>([]);
  const [editingTool, setEditingTool] = useState<HttpTool | null>(null);
  const [loadingTools, setLoadingTools] = useState(false);
  const [deleteToolId, setDeleteToolId] = useState<string | null>(null);

  // Editor Context Variables
  const [contextVars, setContextVars] = useState<ContextVariable[]>([]);
  const promptRef = useRef<HTMLTextAreaElement>(null);

  const { toast } = useToast();

  // LLM Test Integration
  const [testingLLM, setTestingLLM] = useState(false);
  const [testResult, setTestResult] = useState<{
    status: 'success' | 'error';
    message: string;
  } | null>(null);

  const isCreateMode = !agentId;

  // Navegação vertical de seções (substitui as Tabs horizontais do modal)
  const [activeSection, setActiveSection] = useState<AgentConfigSectionId>(() =>
    SECTION_IDS.includes(initialSection as AgentConfigSectionId)
      ? (initialSection as AgentConfigSectionId)
      : 'identity',
  );

  const sections: {
    id: AgentConfigSectionId;
    label: string;
    icon: typeof Settings;
    visible: boolean;
  }[] = [
    { id: 'identity', label: 'Identidade', icon: Settings, visible: true },
    { id: 'model', label: 'Modelo', icon: Brain, visible: true },
    { id: 'personality', label: 'Personalidade', icon: Sparkles, visible: true },
    { id: 'attendance', label: 'Atendimento', icon: Timer, visible: !isSubagent },
    { id: 'memory', label: 'Memória', icon: Database, visible: !isSubagent },
    { id: 'security', label: 'Segurança', icon: Shield, visible: !isSubagent },
    { id: 'tools', label: 'Ferramentas', icon: Plug, visible: true },
    { id: 'mcp', label: 'MCP', icon: Terminal, visible: true },
    { id: 'commerce', label: 'Commerce', icon: ShoppingCart, visible: !isSubagent },
    { id: 'widget', label: 'Widget', icon: Code, visible: !isSubagent },
    { id: 'whatsapp', label: 'WhatsApp', icon: MessageCircle, visible: !isSubagent },
    { id: 'subagents', label: 'Especialistas', icon: Users, visible: !isSubagent && !isCreateMode },
  ];

  // Se a seção ativa deixar de existir (ex.: agente vira SubAgent), volta p/ Identidade
  useEffect(() => {
    const hiddenForSubagent: AgentConfigSectionId[] = [
      'attendance',
      'memory',
      'security',
      'commerce',
      'widget',
      'whatsapp',
      'subagents',
    ];
    if (isSubagent && hiddenForSubagent.includes(activeSection)) {
      setActiveSection('identity');
    }
    if (isCreateMode && activeSection === 'subagents') {
      setActiveSection('identity');
    }
  }, [isSubagent, isCreateMode, activeSection]);

  // Conservative capabilities for legacy/unknown models that aren't in the
  // selectable catalog: keep them usable without exposing advanced controls.
  const FALLBACK_CAPABILITIES: ModelCapabilities = {
    temperature: true,
    reasoning_effort: false,
    thinking: false,
    thinking_api: null,
    vision: false,
    tools: true,
    verbosity: false,
  };

  // Dropdown options: the catalog list plus, when editing an agent whose model
  // is a legacy/non-selectable id, a synthetic entry so the current value is
  // never silently lost.
  const modelOptions = useMemo<CatalogEntry[]>(() => {
    if (!llmModel) return models;
    if (models.some((m) => m.model_id === llmModel)) return models;
    const synthetic: CatalogEntry = {
      model_id: llmModel,
      provider: llmProvider || '',
      label: llmModel,
      tier: null,
      recommended: false,
      selectable: true,
      capabilities: FALLBACK_CAPABILITIES,
      pricing: { input_per_million: null, output_per_million: null, unit: 'per_million_tokens' },
    };
    return [synthetic, ...models];
  }, [models, llmModel, llmProvider]);

  // Selected model + its capabilities drive the dynamic UI gating.
  const selectedModel = useMemo(
    () => modelOptions.find((m) => m.model_id === llmModel),
    [modelOptions, llmModel],
  );
  const selectedCaps = selectedModel?.capabilities;

  useEffect(() => {
    loadProviders();
    if (agentId) {
      loadAgent();
      loadHttpTools();
    } else {
      resetForm();
    }
  }, [agentId]);

  useEffect(() => {
    if (llmProvider) {
      loadModels(llmProvider);
    }
  }, [llmProvider]);

  // Auto-generate slug from name
  useEffect(() => {
    if (isCreateMode && name) {
      const autoSlug = name
        .toLowerCase()
        .normalize('NFD')
        .replace(/[\u0300-\u036f]/g, '')
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-+|-+$/g, '');
      setSlug(autoSlug);
    }
  }, [name, isCreateMode]);

  const resetForm = () => {
    setName('');
    setSlug('');
    setLlmProvider(undefined);
    setLlmModel(undefined);
    setTemperature(0.7);
    setMaxTokens(2000);
    setTopP(1.0);
    setTopK(40);
    setFrequencyPenalty(0.0);
    setPresencePenalty(0.0);
    setSystemPrompt('');
    setAllowWebSearch(true);
    setAllowVision(false);
    setVisionModel(undefined);
    setVisionModel(undefined);
    // removed setVisionApiKey, setHasApiKey, setHasVisionApiKey
    setAllowHumanHandoff(false);
    setContextVars([]);
    setReasoningEffort('medium');
    setVerbosity('medium');
    setThinkingEnabled(false);
    setIsHydeEnabled(true); // Reset HyDE to default

    // Reset Security
    setSecurityEnabled(false);
    setCheckJailbreak(true);
    setCheckNsfw(true);
    setPiiAction('mask');
    setCheckSecretKeys(true);
    setCheckUrls(false);
    setUrlWhitelist('');
    setAllowedTopics('');
    setCustomRegex('');
    setSecurityErrorMessage('Sua mensagem viola as políticas de segurança.');
  };

  const handleTestLLM = async () => {
    if (!llmProvider || !llmModel) {
      toast({
        title: 'Atenção',
        description: 'Selecione um provider e modelo primeiro',
        variant: 'destructive',
      });
      return;
    }

    setTestingLLM(true);
    setTestResult(null);

    try {
      const response = await fetch(`/api/admin/proxy/agents/test-llm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider: llmProvider,
          model: llmModel,
          agent_id: agentId,
          company_id: companyId,
        }),
      });

      const result = await response.json();

      if (response.ok) {
        setTestResult({ status: 'success', message: result.message });
        toast({
          title: 'Sucesso',
          description: result.message,
        });
      } else {
        setTestResult({ status: 'error', message: 'Falha ao testar integração' });
        toast({
          title: 'Erro',
          description: 'Não foi possível testar a integração.',
          variant: 'destructive',
        });
      }
    } catch (error: any) {
      console.error('Error testing LLM:', error);
      setTestResult({ status: 'error', message: 'Falha ao testar integração' });
      toast({
        title: 'Erro',
        description: 'Falha ao conectar com o servidor',
        variant: 'destructive',
      });
    } finally {
      setTestingLLM(false);
    }
  };

  const loadProviders = async () => {
    try {
      const response = await fetch(`/api/admin/proxy/agent/providers`);
      if (response.ok) {
        const data = await response.json();
        setProviders(data);
      }
    } catch (error) {
      console.error('Error loading providers:', error);
    }
  };

  const loadModels = async (provider: string) => {
    // /catalog/{provider} já retorna a lista selecionável correta para TODOS
    // os providers (incluindo openrouter). Sem casos especiais.
    try {
      const response = await fetch(`/api/admin/proxy/agent/catalog/${provider}`);
      if (response.ok) {
        const data: CatalogEntry[] = await response.json();
        setModels(Array.isArray(data) ? data : []);
      } else {
        setModels([]);
      }
    } catch (error) {
      console.error('Error loading models from catalog:', error);
      setModels([]);
    }
    setModelSearch('');
  };

  const loadAgent = async () => {
    if (!agentId) return;

    setLoading(true);
    try {
      const response = await fetch(`/api/admin/proxy/agents/${agentId}`);
      if (response.ok) {
        const agent: Agent = await response.json();

        // Identity
        setName(agent.name);
        setSlug(agent.slug);
        setAvatarUrl(agent.avatar_url || '');

        // LLM Config
        setLlmProvider(agent.llm_provider);
        setLlmModel(agent.llm_model);
        setTemperature(agent.llm_temperature);
        setMaxTokens(agent.llm_max_tokens);
        setTopP(agent.llm_top_p);
        setTopK(agent.llm_top_k);
        setFrequencyPenalty(agent.llm_frequency_penalty);
        setPresencePenalty(agent.llm_presence_penalty);

        // Behavior
        setSystemPrompt(agent.agent_system_prompt || '');
        setAllowWebSearch(agent.allow_web_search);
        setAllowVision(agent.allow_vision);
        setVisionModel(agent.vision_model);
        setIsHydeEnabled(agent.is_hyde_enabled ?? false); // Load HyDE toggle (default OFF)

        // Tools Config
        const toolsConfig = agent.tools_config || {};
        // Guarda o tools_config completo para deep-merge no save (preserva
        // end_attendance e chaves desconhecidas — S5).
        loadedToolsConfigRef.current = (toolsConfig as Record<string, unknown>) || {};
        setAllowHumanHandoff(toolsConfig.human_handoff?.enabled || false);
        setAllowCsvAnalytics(toolsConfig.csv_analytics?.enabled || false);

        // Advanced Config (GPT-5.x, o1, o3)
        setReasoningEffort(agent.reasoning_effort || 'medium');
        setVerbosity(agent.verbosity || 'medium');
        setThinkingEnabled(agent.thinking_enabled ?? false);

        // SubAgent Config
        setIsSubagent(agent.is_subagent ?? false);
        setAllowDirectChat(agent.allow_direct_chat ?? false);

        // Widget Config
        setWidgetConfig(agent.widget_config || {});

        // Security Config
        const sec = (agent.security_settings || {}) as SecuritySettings;
        setSecurityEnabled(sec.enabled ?? false);
        setCheckJailbreak(sec.check_jailbreak ?? true);
        setFailClose(sec.fail_close ?? true);
        setCheckNsfw(sec.check_nsfw ?? true);
        setPiiAction(sec.pii_action || 'mask');
        setCheckSecretKeys(sec.check_secret_keys ?? true);

        // URL Protection
        if (sec.url_protection_mode) {
          setUrlMode(sec.url_protection_mode);
        } else {
          // Fallback/Legacy logic
          setUrlMode(sec.check_urls ? 'whitelist' : 'off');
        }

        setUrlWhitelist(Array.isArray(sec.url_whitelist) ? sec.url_whitelist.join('\n') : '');
        setUrlBlacklist(Array.isArray(sec.url_blacklist) ? sec.url_blacklist.join('\n') : '');

        setAllowedTopics(Array.isArray(sec.allowed_topics) ? sec.allowed_topics.join('\n') : '');
        setCustomRegex(Array.isArray(sec.custom_regex) ? sec.custom_regex.join('\n') : '');
        setSecurityErrorMessage(
          sec.error_message || 'Sua mensagem viola as políticas de segurança.',
        );

        // Load WhatsApp integration for this agent
        await loadWhatsappIntegration(agentId);

        // Load editor context variables
        await loadEditorContext(agentId);
      }
    } catch (error) {
      console.error('Error loading agent:', error);
      toast({
        title: 'Erro',
        description: 'Falha ao carregar configuração do agente',
        variant: 'destructive',
      });
    } finally {
      setLoading(false);
    }
  };

  const loadWhatsappIntegration = async (agentId: string) => {
    try {
      // Use API route with Service Role Key to bypass RLS.
      // companyId é OBRIGATÓRIO p/ master admin (sessão não tem empresa fixa) —
      // sem ele a rota responde 400 "Contexto de empresa obrigatório".
      const response = await fetch(
        `/api/admin/integrations?agentId=${agentId}&companyId=${companyId}`,
      );
      const result = await response.json();

      if (!response.ok) {
        console.error('Error loading integration:', result.error);
        return;
      }

      const integration = result.integration;

      // Base da URL de webhook montada server-side (NEXT_PUBLIC_API_URL público).
      // Ausente quando a flag webhook_url_unavailable está setada (SPEC §1.3/§4.3):
      // nesse caso fica vazia e a UI mostra o aviso em vez de uma URL localhost.
      setWebhookUrlBase(result.webhook_url_unavailable ? '' : result.webhookUrlBase || '');

      if (integration) {
        setHasExistingIntegration(true);
        setIntegrationId(integration.id);
        setWhatsappProvider(integration.provider || 'z-api');
        setWhatsappIdentifier(integration.identifier || '');
        setWhatsappInstanceId(integration.instance_id || '');
        setWhatsappToken(integration.token || '');
        setWhatsappClientToken(integration.client_token || '');
        setWhatsappBaseUrl(integration.base_url || 'https://api.z-api.io/instances');
        setWhatsappIsActive(integration.is_active ?? true);
        setWhatsappBufferEnabled(integration.buffer_enabled ?? true);
        setWhatsappBufferDebounce(integration.buffer_debounce_seconds ?? 3);
        setWhatsappBufferMaxWait(integration.buffer_max_wait_seconds ?? 10);
        const providerConfig = integration.provider_config || {};
        setWhatsappBusinessAccountId(providerConfig.business_account_id || '');
        setWhatsappWebhookVerifyToken(providerConfig.webhook_verify_token || '');
        setWhatsappWebhookMode(
          integration.whatsapp_webhook_mode === 'active' ? 'active' : 'shadow',
        );
        // Token em texto puro só para re-exibir a URL (nunca logado — SPEC §1.2).
        setWhatsappWebhookToken(integration.webhook_token || '');
      } else {
        // Reset to defaults
        setHasExistingIntegration(false);
        setIntegrationId(null);
        setWhatsappProvider('none');
        setWhatsappIdentifier('');
        setWhatsappInstanceId('');
        setWhatsappToken('');
        setWhatsappClientToken('');
        setWhatsappBaseUrl('https://api.z-api.io/instances');
        setWhatsappIsActive(true);
        setWhatsappBufferEnabled(true);
        setWhatsappBufferDebounce(3);
        setWhatsappBufferMaxWait(10);
        setWhatsappBusinessAccountId('');
        setWhatsappWebhookVerifyToken('');
        setWhatsappWebhookMode('shadow');
        setWhatsappWebhookToken('');
      }
    } catch (error) {
      console.error('Error loading WhatsApp integration:', error);
    }
  };

  const loadEditorContext = async (agentId: string) => {
    try {
      const response = await fetch(`/api/admin/proxy/agents/${agentId}/editor-context`);
      if (response.ok) {
        const data = await response.json();
        setContextVars(data.variables || []);
      }
    } catch (error) {
      console.error('Error loading editor context:', error);
    }
  };

  const insertVariable = (tag: string) => {
    const textarea = promptRef.current;
    if (!textarea) return;

    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const newText = systemPrompt.slice(0, start) + tag + systemPrompt.slice(end);
    setSystemPrompt(newText);

    // Reset cursor position after tag
    setTimeout(() => {
      textarea.focus();
      textarea.setSelectionRange(start + tag.length, start + tag.length);
    }, 0);
  };

  // §7.2: ajusta o default de base_url ao trocar de provider.
  // uazapi não usa o host z-api (backend exige base_url não vazio p/ uazapi);
  // z-api restaura o default quando o campo está vazio.
  const handleWhatsappProviderChange = (value: string) => {
    setWhatsappProvider(value);
    if (value === 'uazapi' || value === 'evolution') {
      // uazapi/evolution apontam para servidor próprio: se o base_url ainda é o
      // default z-api, limpa para o admin preencher o host da própria instância.
      setWhatsappBaseUrl((prev) => (prev.trim() === ZAPI_DEFAULT_BASE_URL ? '' : prev));
    } else if (value === 'meta-cloud') {
      setWhatsappBaseUrl((prev) =>
        !prev.trim() || prev.trim() === ZAPI_DEFAULT_BASE_URL
          ? 'https://graph.facebook.com/v23.0'
          : prev,
      );
      setWhatsappWebhookMode('shadow');
    } else if (value === 'z-api') {
      // Voltando para z-api com campo vazio, restaura o default z-api.
      setWhatsappBaseUrl((prev) => (prev.trim() === '' ? ZAPI_DEFAULT_BASE_URL : prev));
    }
  };

  const handleSaveWhatsapp = async () => {
    if (!agentId) {
      toast({
        title: 'Atenção',
        description: 'Salve o agente primeiro antes de configurar o WhatsApp',
        variant: 'destructive',
      });
      return;
    }

    if (whatsappProvider === 'none') {
      // Se mudou para "nenhum" e tinha integração, deletar
      if (hasExistingIntegration && integrationId) {
        setSavingWhatsapp(true);
        try {
          const response = await fetch(`/api/admin/integrations?id=${integrationId}`, {
            method: 'DELETE',
          });

          if (!response.ok) {
            throw new Error('Failed to delete');
          }

          setHasExistingIntegration(false);
          setIntegrationId(null);
          toast({
            title: 'Sucesso',
            description: 'Integração WhatsApp removida',
          });
        } catch (error: any) {
          console.error('Error removing WhatsApp integration:', error);
          toast({
            title: 'Erro',
            description: 'Não foi possível remover a integração.',
            variant: 'destructive',
          });
        } finally {
          setSavingWhatsapp(false);
        }
      }
      return;
    }

    // Validações — comuns
    if (!whatsappIdentifier.trim()) {
      toast({ title: 'Atenção', description: 'Telefone é obrigatório', variant: 'destructive' });
      return;
    }
    if (!whatsappToken.trim()) {
      toast({ title: 'Atenção', description: 'Token é obrigatório', variant: 'destructive' });
      return;
    }

    // Validações específicas por provider (§7.2).
    if (whatsappProvider === 'z-api') {
      // Z-API exige Instance ID; uazapi não o usa.
      if (!whatsappInstanceId.trim()) {
        toast({
          title: 'Atenção',
          description: 'Instance ID é obrigatório',
          variant: 'destructive',
        });
        return;
      }
    } else if (whatsappProvider === 'uazapi') {
      // uazapi exige o host da instância (base_url); o backend rejeita base_url vazio.
      if (!whatsappBaseUrl.trim()) {
        toast({
          title: 'Atenção',
          description: 'Host da instância é obrigatório para uazapi',
          variant: 'destructive',
        });
        return;
      }
    } else if (whatsappProvider === 'evolution') {
      // evolution exige host da instância (base_url) E instance_id; o backend
      // rejeita ambos vazios (route.ts §evolution).
      if (!whatsappBaseUrl.trim()) {
        toast({
          title: 'Atenção',
          description: 'Host da instância é obrigatório para Evolution API',
          variant: 'destructive',
        });
        return;
      }
      if (!whatsappInstanceId.trim()) {
        toast({
          title: 'Atenção',
          description: 'Instance ID é obrigatório para Evolution API',
          variant: 'destructive',
        });
        return;
      }
    } else if (whatsappProvider === 'meta-cloud') {
      if (!whatsappInstanceId.trim()) {
        toast({
          title: 'Atenção',
          description: 'Phone Number ID é obrigatório para Meta Cloud',
          variant: 'destructive',
        });
        return;
      }
      if (!whatsappClientToken.trim()) {
        toast({
          title: 'Atenção',
          description: 'App Secret é obrigatório para Meta Cloud',
          variant: 'destructive',
        });
        return;
      }
      if (!whatsappBusinessAccountId.trim()) {
        toast({
          title: 'Atenção',
          description: 'WABA ID é obrigatório para Meta Cloud',
          variant: 'destructive',
        });
        return;
      }
      if (!whatsappWebhookVerifyToken.trim()) {
        toast({
          title: 'Atenção',
          description: 'Verify Token é obrigatório para Meta Cloud',
          variant: 'destructive',
        });
        return;
      }
    }

    setSavingWhatsapp(true);
    try {
      const payload = {
        agent_id: agentId,
        company_id: companyId,
        provider: whatsappProvider,
        identifier: whatsappIdentifier.trim(),
        instance_id: whatsappInstanceId.trim(),
        token: whatsappToken.trim(),
        client_token: whatsappClientToken.trim() || null,
        base_url: whatsappBaseUrl.trim(),
        provider_config: {
          business_account_id: whatsappBusinessAccountId.trim(),
          webhook_verify_token: whatsappWebhookVerifyToken.trim(),
          graph_version: whatsappBaseUrl.trim().split('/').pop() || 'v23.0',
        },
        whatsapp_webhook_mode: whatsappProvider === 'meta-cloud' ? whatsappWebhookMode : null,
        is_active: whatsappIsActive,
        buffer_enabled: whatsappBufferEnabled,
        buffer_debounce_seconds: whatsappBufferDebounce,
        buffer_max_wait_seconds: whatsappBufferMaxWait,
      };

      // Use API route with Service Role Key to bypass RLS
      const response = await fetch('/api/admin/integrations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      const result = await response.json();

      if (!response.ok) {
        throw new Error('Failed to save');
      }

      if (result.integration) {
        setIntegrationId(result.integration.id);
        setHasExistingIntegration(true);
        // FIX: refletir o token + a base recém-gerados no state, igual ao load do GET
        // (~linha 564) e ao regenerate (~linha 811). Sem isto, o POST cria o token no
        // banco mas a UI não atualiza, e a URL do webhook só aparecia após um reload
        // manual da página — daí parecer que "não dá pra gerar o token".
        setWhatsappWebhookToken(result.integration.webhook_token || '');
        setWebhookUrlBase(result.webhook_url_unavailable ? '' : result.webhookUrlBase || '');
      }

      toast({
        title: 'Sucesso',
        description: 'Integração WhatsApp salva com sucesso!',
      });
    } catch (error: any) {
      console.error('Error saving WhatsApp:', error);
      toast({
        title: 'Erro',
        description: 'Não foi possível salvar a integração.',
        variant: 'destructive',
      });
    } finally {
      setSavingWhatsapp(false);
    }
  };

  // Regenera o token de webhook da integração (cutover duro — a URL antiga para
  // de funcionar na hora). Devolve token + base novos uma vez (SPEC §3.5/§4.3).
  const handleRegenerateWebhookToken = async () => {
    if (!integrationId) {
      toast({
        title: 'Atenção',
        description: 'Salve a integração antes de regenerar o token',
        variant: 'destructive',
      });
      return;
    }

    setRegeneratingWebhook(true);
    try {
      const response = await fetch('/api/admin/integrations/regenerate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: integrationId, company_id: companyId }),
      });

      const result = await response.json();

      if (!response.ok) {
        throw new Error('Failed to regenerate');
      }

      // Novo token/base devolvidos uma vez — atualiza o estado p/ re-exibir a URL.
      // CONTRATO: o regenerate devolve `webhookUrlBase` (mesma chave do GET); a
      // base é a fonte da reconstrução da URL no WhatsAppSection. Fallback
      // defensivo: se só vier `webhookUrl` (URL completa), extrai o prefixo antes
      // de `/api/v1/webhook/` para nunca colapsar a URL após um regenerate OK.
      const regeneratedBase =
        result.webhookUrlBase ||
        (typeof result.webhookUrl === 'string'
          ? result.webhookUrl.split('/api/v1/webhook/')[0]
          : '');
      setWhatsappWebhookToken(result.webhook_token || '');
      setWebhookUrlBase(result.webhook_url_unavailable ? '' : regeneratedBase || '');

      toast({
        title: 'Token regenerado',
        description:
          'A URL antiga deixou de funcionar. Copie a nova URL e cole no painel do seu provedor.',
      });
    } catch (error: any) {
      console.error('Error regenerating webhook token:', error);
      toast({
        title: 'Erro',
        description: 'Não foi possível regenerar o token do webhook.',
        variant: 'destructive',
      });
    } finally {
      setRegeneratingWebhook(false);
    }
  };

  // ============= HTTP TOOLS CRUD (via secure admin proxy) =============
  const loadHttpTools = async () => {
    if (!agentId) return;
    setLoadingTools(true);
    try {
      const res = await fetch(`/api/admin/proxy/agents/${agentId}/tools?company_id=${companyId}`);
      if (res.ok) {
        const data = await res.json();
        // Converter headers de objeto para array para edição
        const toolsWithArrayHeaders = data.map((t: any) => ({
          ...t,
          headers: Array.isArray(t.headers)
            ? t.headers
            : t.headers
              ? Object.entries(t.headers).map(([key, value]) => ({ key, value }))
              : [],
        }));
        setHttpTools(toolsWithArrayHeaders);
      }
    } catch (e) {
      console.error('Erro ao carregar tools:', e);
    } finally {
      setLoadingTools(false);
    }
  };

  const handleSaveTool = async (tool: HttpTool) => {
    try {
      // Converter array de headers para objeto JSONB
      const headersObj = tool.headers.reduce(
        (acc, curr) => ({ ...acc, [curr.key]: curr.value }),
        {},
      );

      const payload = {
        ...tool,
        headers: headersObj,
        agent_id: agentId,
        is_active: true,
      };

      const method = tool.id ? 'PUT' : 'POST';
      const url = tool.id
        ? `/api/admin/proxy/agents/tools/${tool.id}`
        : '/api/admin/proxy/agents/tools';
      const res = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      if (!res.ok) {
        throw new Error('Falha ao salvar');
      }

      toast({ title: 'Sucesso', description: 'Ferramenta salva com sucesso' });
      await loadHttpTools();
      // Atualiza lista de variáveis do prompt (HTTP tools aparecem lá agora)
      if (agentId) await loadEditorContext(agentId);
      setToolsView('list');
    } catch (error: any) {
      console.error('🔧 ERROR:', error);
      toast({
        title: 'Erro',
        description: 'Não foi possível salvar a ferramenta.',
        variant: 'destructive',
      });
    }
  };

  const handleDeleteTool = async (toolId: string) => {
    try {
      const response = await fetch(`/api/admin/proxy/agents/tools/${toolId}`, {
        method: 'DELETE',
      });
      if (!response.ok) {
        throw new Error('Failed to delete tool');
      }
      toast({ title: 'Deletado', description: 'Ferramenta removida' });
      await loadHttpTools();
      // Atualiza lista de variáveis do prompt (remove tool deletada)
      if (agentId) await loadEditorContext(agentId);
    } catch (e) {
      toast({ title: 'Erro', description: 'Erro ao deletar', variant: 'destructive' });
    }
  };

  const handleSave = async () => {
    // Validation
    if (!name.trim()) {
      toast({
        title: 'Atenção',
        description: 'Nome do agente é obrigatório',
        variant: 'destructive',
      });
      return;
    }

    if (!slug.trim()) {
      toast({
        title: 'Atenção',
        description: 'Slug é obrigatório',
        variant: 'destructive',
      });
      return;
    }

    if (!llmProvider || !llmModel) {
      toast({
        title: 'Atenção',
        description: 'Selecione um provider e modelo',
        variant: 'destructive',
      });
      return;
    }

    setSaving(true);
    try {
      const payload = {
        company_id: companyId,
        name: name.trim(),
        slug: slug.trim(),
        avatar_url: avatarUrl,
        llm_provider: llmProvider,
        llm_model: llmModel,
        // Modelos sem suporte a temperatura customizada são fixados em 1.0
        llm_temperature: selectedCaps?.temperature === false ? 1.0 : temperature,
        llm_max_tokens: maxTokens,
        llm_top_p: topP,
        llm_top_k: topK,
        llm_frequency_penalty: frequencyPenalty,
        llm_presence_penalty: presencePenalty,
        agent_system_prompt: systemPrompt || DEFAULT_SYSTEM_PROMPT,
        allow_web_search: allowWebSearch,
        allow_vision: allowVision,
        vision_model: visionModel,
        // Deep-merge (S5/S10): preserva chaves não editadas por esta UI
        // (end_attendance e quaisquer chaves desconhecidas/futuras). Apenas
        // human_handoff/csv_analytics são sobrescritos com os toggles atuais —
        // ligar end_attendance na aba Atendimento NÃO é apagado por um save aqui,
        // pois `loadedToolsConfigRef` é re-sincronizado no `onAttendanceSaved`.
        // A lógica do merge é a função PURA `mergeGeneralSaveToolsConfig`
        // (ÚNICO site, coberto pelo teste obrigatório §9.3/§18.1).
        tools_config: mergeGeneralSaveToolsConfig(loadedToolsConfigRef.current, {
          handoffEnabled: allowHumanHandoff,
          csvAnalyticsEnabled: allowCsvAnalytics,
        }),
        reasoning_effort: selectedCaps?.reasoning_effort ? reasoningEffort : 'none',
        verbosity: selectedCaps?.verbosity ? verbosity : 'medium',
        thinking_enabled: selectedCaps?.thinking ? thinkingEnabled : false,
        is_hyde_enabled: isHydeEnabled,
        is_subagent: isSubagent,
        allow_direct_chat: allowDirectChat,
        widget_config: widgetConfig, // Include widget config in save
        security_settings: {
          enabled: securityEnabled,
          fail_close: failClose,
          check_jailbreak: checkJailbreak,
          check_nsfw: false, // Desativado — Llama Guard 4 descontinuado (Mar/2026)
          pii_action: piiAction,
          check_secret_keys: checkSecretKeys,
          check_urls: urlMode !== 'off', // Legacy compatibility
          url_protection_mode: urlMode,
          url_whitelist: urlWhitelist
            .split('\n')
            .map((s) => s.trim())
            .filter(Boolean),
          url_blacklist: urlBlacklist
            .split('\n')
            .map((s) => s.trim())
            .filter(Boolean),
          allowed_topics: allowedTopics
            .split('\n')
            .map((s) => s.trim())
            .filter(Boolean),
          custom_regex: customRegex
            .split('\n')
            .map((s) => s.trim())
            .filter(Boolean),
          error_message: securityErrorMessage,
        },
      };

      const url = isCreateMode ? `/api/admin/proxy/agents` : `/api/admin/proxy/agents/${agentId}`;

      const method = isCreateMode ? 'POST' : 'PUT';

      const response = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      if (response.ok) {
        toast({
          title: 'Sucesso',
          description: isCreateMode ? 'Agente criado com sucesso' : 'Agente atualizado com sucesso',
        });
        if (isCreateMode) {
          // onSaved(agentId) — usado pelo call site p/ redirecionar à tela de edição
          const created = await response.json().catch(() => null);
          const newAgentId = created?.id;
          if (newAgentId && onSaved) {
            onSaved(String(newAgentId));
          }
        }
      } else {
        toast({
          title: 'Erro',
          description: 'Não foi possível salvar o agente.',
          variant: 'destructive',
        });
      }
    } catch (error) {
      toast({
        title: 'Erro',
        description: 'Erro ao salvar agente',
        variant: 'destructive',
      });
    } finally {
      setSaving(false);
    }
  };

  const visibleSections = sections.filter((s) => s.visible);

  return (
    <>
      <div className="space-y-6">
        {/* Header */}
        <div className="flex items-center gap-3">
          <Button
            variant="ghost"
            size="icon"
            onClick={onBack}
            className="text-muted-foreground hover:text-foreground"
            aria-label="Voltar"
          >
            <ArrowLeft className="h-5 w-5" />
          </Button>
          <h1 className="flex items-center gap-2 text-xl font-semibold text-foreground">
            <Settings className="h-5 w-5" />
            {isCreateMode ? 'Criar Novo Agente' : `Editar Agente: ${name}`}
          </h1>
        </div>

        {loading ? (
          <div className="flex items-center justify-center p-8">
            <Loader2 className="h-8 w-8 animate-spin text-primary" />
          </div>
        ) : (
          <div className="flex flex-col md:flex-row gap-6 items-start">
            {/* Navegação vertical de seções (segunda coluna ao lado da sidebar do admin) */}
            <nav className="w-full md:w-56 shrink-0 md:sticky md:top-6 space-y-1 bg-muted/50 p-2 rounded-md">
              {visibleSections.map((section) => {
                const Icon = section.icon;
                const isActive = activeSection === section.id;
                return (
                  <button
                    key={section.id}
                    type="button"
                    onClick={() => setActiveSection(section.id)}
                    className={`w-full flex items-center gap-2 rounded-md px-3 py-2 text-sm text-left transition-colors ${
                      isActive
                        ? 'bg-primary text-primary-foreground'
                        : 'text-muted-foreground hover:bg-muted hover:text-foreground'
                    }`}
                  >
                    <Icon className="h-4 w-4 shrink-0" />
                    {section.label}
                  </button>
                );
              })}
            </nav>

            {/* Conteúdo da seção ativa */}
            <div className="flex-1 min-w-0 w-full">
              {activeSection === 'identity' && (
                <IdentitySection
                  name={name}
                  setName={setName}
                  slug={slug}
                  setSlug={setSlug}
                  isSubagent={isSubagent}
                  setIsSubagent={setIsSubagent}
                  allowDirectChat={allowDirectChat}
                  setAllowDirectChat={setAllowDirectChat}
                />
              )}

              {activeSection === 'model' && (
                <ModelSection
                  providers={providers}
                  modelOptions={modelOptions}
                  selectedCaps={selectedCaps}
                  llmProvider={llmProvider}
                  setLlmProvider={setLlmProvider}
                  llmModel={llmModel}
                  setLlmModel={setLlmModel}
                  modelSearch={modelSearch}
                  setModelSearch={setModelSearch}
                  temperature={temperature}
                  setTemperature={setTemperature}
                  maxTokens={maxTokens}
                  setMaxTokens={setMaxTokens}
                  topP={topP}
                  setTopP={setTopP}
                  topK={topK}
                  setTopK={setTopK}
                  frequencyPenalty={frequencyPenalty}
                  setFrequencyPenalty={setFrequencyPenalty}
                  presencePenalty={presencePenalty}
                  setPresencePenalty={setPresencePenalty}
                  reasoningEffort={reasoningEffort}
                  setReasoningEffort={setReasoningEffort}
                  verbosity={verbosity}
                  setVerbosity={setVerbosity}
                  thinkingEnabled={thinkingEnabled}
                  setThinkingEnabled={setThinkingEnabled}
                  testingLLM={testingLLM}
                  testResult={testResult}
                  onTestLLM={handleTestLLM}
                />
              )}

              {activeSection === 'personality' && (
                <PersonalitySection
                  agentId={agentId}
                  isCreateMode={isCreateMode}
                  isSubagent={isSubagent}
                  name={name}
                  avatarUrl={avatarUrl}
                  setAvatarUrl={setAvatarUrl}
                  contextVars={contextVars}
                  insertVariable={insertVariable}
                  promptRef={promptRef}
                  systemPrompt={systemPrompt}
                  setSystemPrompt={setSystemPrompt}
                  allowWebSearch={allowWebSearch}
                  setAllowWebSearch={setAllowWebSearch}
                  allowHumanHandoff={allowHumanHandoff}
                  setAllowHumanHandoff={setAllowHumanHandoff}
                  allowCsvAnalytics={allowCsvAnalytics}
                  setAllowCsvAnalytics={setAllowCsvAnalytics}
                  allowVision={allowVision}
                  setAllowVision={setAllowVision}
                  visionModel={visionModel}
                  setVisionModel={setVisionModel}
                />
              )}

              {activeSection === 'attendance' && !isSubagent && (
                <AttendanceSection
                  agentId={agentId}
                  isSubagent={isSubagent}
                  allowHumanHandoff={allowHumanHandoff}
                  setAllowHumanHandoff={setAllowHumanHandoff}
                  onAttendanceSaved={({ handoffEnabled, agentCanClose }) => {
                    // Mantém o snapshot de tools_config do save GERAL em sincronia
                    // com o que a aba Atendimento acabou de gravar (§24): sem isto,
                    // o ref STALE zeraria end_attendance.enabled num save posterior
                    // em outra aba. Espelha as DUAS chaves de atendimento.
                    loadedToolsConfigRef.current = {
                      ...loadedToolsConfigRef.current,
                      human_handoff: {
                        ...(loadedToolsConfigRef.current.human_handoff as
                          | Record<string, unknown>
                          | undefined),
                        enabled: handoffEnabled,
                      },
                      end_attendance: {
                        ...(loadedToolsConfigRef.current.end_attendance as
                          | Record<string, unknown>
                          | undefined),
                        enabled: agentCanClose,
                      },
                    };
                  }}
                />
              )}

              {activeSection === 'memory' && !isSubagent && <MemorySection agentId={agentId} />}

              {activeSection === 'security' && !isSubagent && (
                <SecuritySection
                  securityEnabled={securityEnabled}
                  setSecurityEnabled={setSecurityEnabled}
                  checkJailbreak={checkJailbreak}
                  setCheckJailbreak={setCheckJailbreak}
                  piiAction={piiAction}
                  setPiiAction={setPiiAction}
                  checkSecretKeys={checkSecretKeys}
                  setCheckSecretKeys={setCheckSecretKeys}
                  failClose={failClose}
                  setFailClose={setFailClose}
                  urlMode={urlMode}
                  setUrlMode={setUrlMode}
                  urlWhitelist={urlWhitelist}
                  setUrlWhitelist={setUrlWhitelist}
                  urlBlacklist={urlBlacklist}
                  setUrlBlacklist={setUrlBlacklist}
                  allowedTopics={allowedTopics}
                  setAllowedTopics={setAllowedTopics}
                  customRegex={customRegex}
                  setCustomRegex={setCustomRegex}
                  securityErrorMessage={securityErrorMessage}
                  setSecurityErrorMessage={setSecurityErrorMessage}
                />
              )}

              {activeSection === 'tools' && (
                <ToolsSection
                  agentId={agentId}
                  companyId={companyId}
                  isHydeEnabled={isHydeEnabled}
                  setIsHydeEnabled={setIsHydeEnabled}
                  toolsView={toolsView}
                  setToolsView={setToolsView}
                  httpTools={httpTools}
                  editingTool={editingTool}
                  setEditingTool={setEditingTool}
                  loadingTools={loadingTools}
                  setDeleteToolId={setDeleteToolId}
                  onSaveTool={handleSaveTool}
                  onToolsChanged={() => {
                    if (agentId) loadEditorContext(agentId);
                  }}
                />
              )}

              {activeSection === 'mcp' && (
                <MCPSection
                  agentId={agentId}
                  companyId={companyId}
                  onToolsChanged={() => {
                    if (agentId) loadEditorContext(agentId);
                  }}
                />
              )}

              {activeSection === 'commerce' && !isSubagent && (
                <CommerceSection agentId={agentId} companyId={companyId} />
              )}

              {activeSection === 'widget' && !isSubagent && (
                <WidgetSection
                  agentId={agentId}
                  companyId={companyId}
                  name={name}
                  slug={slug}
                  avatarUrl={avatarUrl}
                  temperature={temperature}
                  maxTokens={maxTokens}
                  topP={topP}
                  topK={topK}
                  frequencyPenalty={frequencyPenalty}
                  presencePenalty={presencePenalty}
                  allowWebSearch={allowWebSearch}
                  allowVision={allowVision}
                  hasExistingIntegration={hasExistingIntegration}
                  widgetConfig={widgetConfig}
                  setWidgetConfig={setWidgetConfig}
                />
              )}

              {activeSection === 'whatsapp' && !isSubagent && (
                <WhatsAppSection
                  isCreateMode={isCreateMode}
                  hasExistingIntegration={hasExistingIntegration}
                  whatsappProvider={whatsappProvider}
                  setWhatsappProvider={handleWhatsappProviderChange}
                  whatsappIdentifier={whatsappIdentifier}
                  setWhatsappIdentifier={setWhatsappIdentifier}
                  whatsappInstanceId={whatsappInstanceId}
                  setWhatsappInstanceId={setWhatsappInstanceId}
                  whatsappToken={whatsappToken}
                  setWhatsappToken={setWhatsappToken}
                  whatsappClientToken={whatsappClientToken}
                  setWhatsappClientToken={setWhatsappClientToken}
                  whatsappBaseUrl={whatsappBaseUrl}
                  setWhatsappBaseUrl={setWhatsappBaseUrl}
                  whatsappIsActive={whatsappIsActive}
                  setWhatsappIsActive={setWhatsappIsActive}
                  whatsappBufferEnabled={whatsappBufferEnabled}
                  setWhatsappBufferEnabled={setWhatsappBufferEnabled}
                  whatsappBufferDebounce={whatsappBufferDebounce}
                  setWhatsappBufferDebounce={setWhatsappBufferDebounce}
                  whatsappBufferMaxWait={whatsappBufferMaxWait}
                  setWhatsappBufferMaxWait={setWhatsappBufferMaxWait}
                  whatsappBusinessAccountId={whatsappBusinessAccountId}
                  setWhatsappBusinessAccountId={setWhatsappBusinessAccountId}
                  whatsappWebhookVerifyToken={whatsappWebhookVerifyToken}
                  setWhatsappWebhookVerifyToken={setWhatsappWebhookVerifyToken}
                  whatsappWebhookMode={whatsappWebhookMode}
                  setWhatsappWebhookMode={setWhatsappWebhookMode}
                  savingWhatsapp={savingWhatsapp}
                  onSaveWhatsapp={handleSaveWhatsapp}
                  whatsappWebhookToken={whatsappWebhookToken}
                  webhookUrlBase={webhookUrlBase}
                  regeneratingWebhook={regeneratingWebhook}
                  onRegenerateWebhookToken={handleRegenerateWebhookToken}
                />
              )}

              {activeSection === 'subagents' && !isSubagent && !isCreateMode && agentId && (
                <SubagentsSection agentId={agentId} companyId={companyId} />
              )}

              {/* Action Buttons */}
              <div className="mt-8 mb-12">
                <div className="flex gap-3">
                  <Button
                    onClick={handleSave}
                    disabled={saving}
                    className="flex-1 bg-primary hover:bg-primary/90 text-primary-foreground"
                  >
                    {saving ? (
                      <>
                        <Loader2 className="h-4 w-4 animate-spin mr-2" />
                        Salvando...
                      </>
                    ) : (
                      <>
                        <CheckCircle className="h-4 w-4 mr-2" />
                        {isCreateMode ? 'Criar Agente' : 'Salvar Alterações'}
                      </>
                    )}
                  </Button>
                  <Button onClick={onBack} variant="outline" disabled={saving}>
                    Voltar
                  </Button>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>

      <ConfirmDialog
        open={!!deleteToolId}
        onOpenChange={(isOpen) => {
          if (!isOpen) setDeleteToolId(null);
        }}
        title="Excluir ferramenta?"
        description="Esta ação remove a ferramenta HTTP deste agente."
        confirmLabel="Excluir"
        destructive
        onConfirm={() => {
          const toolId = deleteToolId;
          setDeleteToolId(null);
          if (toolId) {
            void handleDeleteTool(toolId);
          }
        }}
      />
    </>
  );
}
