'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import {
  AlertTriangle,
  Link,
  Loader2,
  Plug,
  RefreshCw,
  Search,
  ToggleLeft,
  ToggleRight,
  Unlink,
  User,
} from 'lucide-react';
import { useToast } from '@/hooks/use-toast';

// =========================================================================
// Tipos (100% data-driven: shapes vêm de /servers, /oauth/providers,
// /agent/{id}/connections e /agent/{id}/tools — zero hardcode de provider)
// =========================================================================

export interface MCPServer {
  id: string;
  name: string;
  display_name: string;
  description?: string;
  server_type?: string; // 'internal' | 'remote' (B5)
  url?: string | null; // display only (B5)
  oauth_provider: string | null;
  provider_configured?: boolean;
}

export interface MCPConnection {
  id: string;
  mcp_server_id: string;
  is_connected: boolean;
  is_active?: boolean;
  connected_at?: string;
  server_type?: string; // (B5)
  connection_config?: Record<string, unknown> | null; // (B5)
  connection_metadata?: Record<string, unknown> | null; // (B2/B5)
  mcp_server?: {
    name: string;
    display_name: string;
    oauth_provider?: string | null;
  };
}

export interface MCPAgentTool {
  id?: string; // agent_mcp_tools.id — necessário para o toggle
  tool_name?: string;
  variable_name?: string;
  display_name?: string;
  description?: string;
  mcp_server_id?: string;
  mcp_server_name?: string;
  is_enabled?: boolean;
  is_available?: boolean;
}

export interface OAuthProviderInfo {
  name?: string;
  configured?: boolean;
  services?: string[];
}

const MCP_API = '/api/admin/proxy/mcp';

// =========================================================================
// Quirks por provider — dirigidos por dados do server (SPEC impl §5.3 itens
// 3-4). Única exceção documentada de comportamento por server: o Supabase
// (name === 'supabase' vindo do catálogo /servers) exige `project_ref` antes
// de liberar tools e expõe modo read-only; o Klaviyo mapeia erro de papel
// insuficiente para mensagem amigável. Todo o resto permanece data-driven.
// =========================================================================

const SUPABASE_SERVER_NAME = 'supabase';
const KLAVIYO_PROVIDER = 'klaviyo';

// Validação client-side do project_ref do Supabase (SPEC impl §4.3).
const SUPABASE_PROJECT_REF_REGEX = /^[a-z0-9]{15,25}$/;

const KLAVIYO_ROLE_MESSAGE = 'a conta usada precisa ser Owner, Admin ou Manager no Klaviyo';

// Heurística para reconhecer erro de papel insuficiente vindo do fluxo
// OAuth do Klaviyo (access_denied / forbidden / menção a role/permissão).
const KLAVIYO_ROLE_ERROR_PATTERN =
  /access[_ ]?denied|forbidden|insufficient|\brole\b|permission|\bowner\b|\badmin\b|\bmanager\b|403/i;

function mapConnectErrorMessage(provider: string | null | undefined, rawMessage: string): string {
  if (provider === KLAVIYO_PROVIDER && KLAVIYO_ROLE_ERROR_PATTERN.test(rawMessage)) {
    return KLAVIYO_ROLE_MESSAGE;
  }
  return rawMessage || 'Falha ao conectar';
}

// Chaves genéricas (independentes de provider) para extrair a identidade
// da conexão a partir de connection_metadata.
const IDENTITY_KEYS = [
  'workspace_name',
  'workspace',
  'account_name',
  'account',
  'organization_name',
  'organization',
  'team_name',
  'team',
  'user_email',
  'email',
  'user_name',
  'username',
  'name',
];

function getConnectionIdentity(metadata?: Record<string, unknown> | null): string | null {
  if (!metadata) return null;
  for (const key of IDENTITY_KEYS) {
    const value = metadata[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return null;
}

function getToolLabel(tool: MCPAgentTool): string {
  return tool.tool_name || tool.variable_name || tool.display_name || 'tool';
}

// =========================================================================
// Card por MCP server
// =========================================================================

interface McpServerCardProps {
  server: MCPServer;
  connection?: MCPConnection;
  tools: MCPAgentTool[];
  toolsError?: boolean;
  providerInfo?: OAuthProviderInfo;
  busy: boolean;
  onConnect: (server: MCPServer) => void;
  onDisconnect: (server: MCPServer) => void;
  onRefreshTools: (server: MCPServer) => void;
  onToggleTool: (tool: MCPAgentTool, enabled: boolean) => void;
  onToggleAll: (server: MCPServer, enabled: boolean) => void;
  onSaveConfig: (server: MCPServer, config: Record<string, unknown>) => Promise<void>;
  onRetryLoad?: () => void;
}

export function McpServerCard({
  server,
  connection,
  tools,
  toolsError = false,
  providerInfo,
  busy,
  onConnect,
  onDisconnect,
  onRefreshTools,
  onToggleTool,
  onToggleAll,
  onSaveConfig,
  onRetryLoad,
}: McpServerCardProps) {
  const [search, setSearch] = useState('');

  const isConnected = Boolean(connection?.is_connected);
  const providerConfigured = providerInfo?.configured ?? server.provider_configured ?? false;
  const canConnect = Boolean(server.oauth_provider) && providerConfigured;

  const identity = getConnectionIdentity(connection?.connection_metadata);

  // ---- Quirk Supabase: project_ref + read-only (SPEC impl §5.3 item 3) ----
  const isSupabase = server.name === SUPABASE_SERVER_NAME;
  const savedConfig = (connection?.connection_config ?? {}) as Record<string, unknown>;
  const savedProjectRef =
    typeof savedConfig.project_ref === 'string' ? savedConfig.project_ref : '';
  const savedReadOnly = savedConfig.read_only === true;

  const [projectRef, setProjectRef] = useState(savedProjectRef);
  const [readOnly, setReadOnly] = useState(savedReadOnly);
  const [savingConfig, setSavingConfig] = useState(false);

  // Reflete o connection_config persistido ao (re)carregar a conexão.
  useEffect(() => {
    setProjectRef(savedProjectRef);
    setReadOnly(savedReadOnly);
  }, [savedProjectRef, savedReadOnly]);

  const projectRefValid = SUPABASE_PROJECT_REF_REGEX.test(projectRef.trim());
  const configDirty = projectRef.trim() !== savedProjectRef || readOnly !== savedReadOnly;
  const hasSavedProjectRef = SUPABASE_PROJECT_REF_REGEX.test(savedProjectRef);

  // Toggles de tools bloqueados enquanto o project_ref não estiver salvo.
  const toolsLocked = isSupabase && !hasSavedProjectRef;

  const handleSaveConfig = async () => {
    if (!projectRefValid || savingConfig) return;
    setSavingConfig(true);
    try {
      await onSaveConfig(server, {
        project_ref: projectRef.trim(),
        read_only: readOnly,
      });
    } finally {
      setSavingConfig(false);
    }
  };

  const enabledCount = tools.filter((t) => t.is_enabled).length;
  // "Habilitar todas" opera só sobre tools disponíveis no servidor (ligar uma
  // indisponível é no-op no runtime). O botão alterna para "Desabilitar todas"
  // quando todas as disponíveis já estão ligadas.
  const availableTools = tools.filter((t) => t.is_available !== false);
  const allAvailableEnabled =
    availableTools.length > 0 && availableTools.every((t) => t.is_enabled);
  const filteredTools = useMemo(() => {
    const term = search.trim().toLowerCase();
    if (!term) return tools;
    return tools.filter((t) => getToolLabel(t).toLowerCase().includes(term));
  }, [tools, search]);

  return (
    <Card className={`bg-background border ${isConnected ? 'border-primary/40' : 'border-border'}`}>
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-3">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <Plug
                className={`h-4 w-4 shrink-0 ${
                  isConnected ? 'text-primary' : 'text-muted-foreground'
                }`}
              />
              <CardTitle className="text-sm text-foreground">{server.display_name}</CardTitle>
              {server.server_type && (
                <Badge variant="outline" className="text-xs border-border text-muted-foreground">
                  {server.server_type === 'remote' ? 'Remoto' : 'Interno'}
                </Badge>
              )}
              {isConnected && <Badge className="bg-primary text-xs">Conectado</Badge>}
            </div>
            {server.description && (
              <p className="text-xs text-muted-foreground mt-1">{server.description}</p>
            )}
            {server.url && (
              <p className="text-xs text-muted-foreground/70 mt-0.5 font-mono truncate">
                {server.url}
              </p>
            )}
          </div>

          <div className="flex gap-2 shrink-0">
            {!isConnected && canConnect && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => onConnect(server)}
                disabled={busy}
                className="bg-card border-primary text-primary hover:bg-brand-muted"
              >
                {busy ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <>
                    <Link className="h-4 w-4 mr-1" />
                    Conectar
                  </>
                )}
              </Button>
            )}
            {!isConnected && !canConnect && (
              <Button
                variant="outline"
                size="sm"
                disabled
                className="bg-muted border-border text-muted-foreground"
              >
                Indisponível
              </Button>
            )}
            {isConnected && (
              <>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => onRefreshTools(server)}
                  disabled={busy}
                  className="bg-card border-primary text-primary hover:bg-brand-muted"
                >
                  {busy ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <>
                      <RefreshCw className="h-4 w-4 mr-1" />
                      Atualizar tools
                    </>
                  )}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => onDisconnect(server)}
                  disabled={busy}
                  className="text-muted-foreground hover:text-danger"
                  title="Desconectar"
                >
                  <Unlink className="h-4 w-4" />
                </Button>
              </>
            )}
          </div>
        </div>

        {/* Identidade da conexão DESTE agente (isolamento visível na UI) */}
        {isConnected && (
          <div className="flex items-center gap-1.5 mt-2">
            <User className="h-3.5 w-3.5 text-primary" />
            <span className="text-xs text-foreground">{identity || 'Conta conectada'}</span>
            {!identity && (
              <span className="text-xs text-muted-foreground">
                (provider não retornou identificação)
              </span>
            )}
          </div>
        )}

        {/* Aviso fixo do Supabase: sempre visível no card (SPEC impl §5.3) */}
        {isSupabase && (
          <p className="text-xs text-warning mt-2 flex items-center gap-1">
            <AlertTriangle className="h-3 w-3 shrink-0" />
            Atenção: este MCP pode executar SQL com escrita no seu projeto.
          </p>
        )}

        {!isConnected && !canConnect && server.oauth_provider && (
          <p className="text-xs text-danger mt-2 flex items-center gap-1">
            <AlertTriangle className="h-3 w-3" />
            Integração não configurada na plataforma
          </p>
        )}
      </CardHeader>

      {/* Curadoria de tools: lista completa, toggle individual (OFF por padrão) */}
      {isConnected && (
        <CardContent className="pt-0 space-y-3">
          {/* Config por conexão do Supabase: project_ref + modo read-only */}
          {isSupabase && (
            <div className="space-y-3 p-3 rounded border border-border bg-card">
              <div className="space-y-1">
                <label
                  htmlFor={`project-ref-${server.id}`}
                  className="text-xs font-medium text-foreground"
                >
                  project_ref do projeto Supabase
                </label>
                <p className="text-xs text-muted-foreground">
                  Restringe a conexão a um único projeto. Obrigatório antes de habilitar tools.
                </p>
                <Input
                  id={`project-ref-${server.id}`}
                  value={projectRef}
                  onChange={(e) => setProjectRef(e.target.value)}
                  placeholder="ex.: abcdefghijklmnop"
                  disabled={savingConfig || busy}
                  className="h-8 text-xs font-mono bg-background border-border max-w-xs"
                />
                {projectRef.trim() && !projectRefValid && (
                  <p className="text-xs text-danger">
                    project_ref inválido: use 15 a 25 caracteres entre a-z e 0-9.
                  </p>
                )}
              </div>

              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <span className="text-xs font-medium text-foreground">
                    Modo somente leitura (read-only)
                  </span>
                  <p className="text-xs text-muted-foreground">
                    Executa todas as queries como usuário Postgres read-only. Recomendado para dados
                    reais.
                  </p>
                </div>
                <Switch
                  checked={readOnly}
                  onCheckedChange={setReadOnly}
                  disabled={savingConfig || busy}
                />
              </div>

              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleSaveConfig}
                  disabled={!projectRefValid || !configDirty || savingConfig || busy}
                  className="bg-card border-primary text-primary hover:bg-brand-muted"
                >
                  {savingConfig ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    'Salvar configuração'
                  )}
                </Button>
                {hasSavedProjectRef && !configDirty && (
                  <Badge variant="outline" className="text-xs border-border text-muted-foreground">
                    configuração salva
                  </Badge>
                )}
              </div>
            </div>
          )}

          {/* Hint: toggles bloqueados até salvar o project_ref (Supabase) */}
          {toolsLocked && tools.length > 0 && (
            <p className="text-xs text-warning flex items-center gap-1">
              <AlertTriangle className="h-3 w-3 shrink-0" />
              Salve um project_ref válido para liberar as tools deste servidor.
            </p>
          )}

          {toolsError ? (
            <div className="flex flex-col items-start gap-2 p-3 rounded border border-danger/40 bg-danger/5">
              <p className="text-xs text-danger flex items-center gap-1">
                <AlertTriangle className="h-3 w-3 shrink-0" />
                Não foi possível carregar o catálogo de tools. Verifique sua conexão e tente
                novamente.
              </p>
              <Button
                variant="outline"
                size="sm"
                onClick={() => onRetryLoad?.()}
                disabled={busy}
                className="bg-card border-danger text-danger hover:bg-danger/10"
              >
                <RefreshCw className="h-4 w-4 mr-1" />
                Tentar novamente
              </Button>
            </div>
          ) : tools.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              Nenhuma tool descoberta para este servidor. Clique em &quot;Atualizar tools&quot; para
              descobri-las.
            </p>
          ) : (
            <>
              <div className="flex items-center justify-between gap-3">
                <div className="relative flex-1 max-w-xs">
                  <Search className="h-3.5 w-3.5 text-muted-foreground absolute left-2.5 top-1/2 -translate-y-1/2" />
                  <Input
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    placeholder="Buscar tool por nome..."
                    className="h-8 pl-8 text-xs bg-card border-border"
                  />
                </div>
                <div className="flex items-center gap-3 shrink-0">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => onToggleAll(server, !allAvailableEnabled)}
                    disabled={busy || toolsLocked || availableTools.length === 0}
                    className="h-8 text-xs bg-card border-border text-foreground hover:bg-muted"
                  >
                    {busy ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : allAvailableEnabled ? (
                      <>
                        <ToggleLeft className="h-3.5 w-3.5 mr-1" />
                        Desabilitar todas
                      </>
                    ) : (
                      <>
                        <ToggleRight className="h-3.5 w-3.5 mr-1" />
                        Habilitar todas
                      </>
                    )}
                  </Button>
                  <span className="text-xs text-muted-foreground">
                    {enabledCount} de {tools.length} habilitadas
                  </span>
                </div>
              </div>

              <div className="space-y-2 max-h-80 overflow-y-auto pr-1">
                {filteredTools.length === 0 ? (
                  <p className="text-xs text-muted-foreground py-2">
                    Nenhuma tool encontrada para &quot;{search}&quot;.
                  </p>
                ) : (
                  filteredTools.map((tool) => {
                    const available = tool.is_available !== false;
                    // Edge case: tool ligada na curadoria, porém indisponível no servidor.
                    // O Switch permanece ON e disabled, mas a tool NÃO entra no runtime.
                    const enabledButUnavailable = !available && Boolean(tool.is_enabled);
                    return (
                      <div
                        key={tool.id || getToolLabel(tool)}
                        className={`flex items-center justify-between gap-3 p-2.5 rounded border border-border bg-card ${
                          available ? '' : 'opacity-50'
                        }`}
                      >
                        <div className="min-w-0">
                          <div className="flex items-center gap-2 flex-wrap">
                            <code className="text-xs font-mono text-foreground">
                              {getToolLabel(tool)}
                            </code>
                            {!available && (
                              <Badge
                                variant="outline"
                                className="text-xs border-border text-muted-foreground"
                              >
                                indisponível no servidor
                              </Badge>
                            )}
                          </div>
                          {tool.description && (
                            <p className="text-xs text-muted-foreground mt-0.5 line-clamp-2">
                              {tool.description}
                            </p>
                          )}
                          {enabledButUnavailable && (
                            <p className="text-xs text-warning mt-1 flex items-center gap-1">
                              <AlertTriangle className="h-3 w-3 shrink-0" />
                              Tool ligada, mas indisponível no servidor: ela não entra no runtime do
                              agente até voltar a ser descoberta.
                            </p>
                          )}
                        </div>
                        <Switch
                          checked={Boolean(tool.is_enabled)}
                          disabled={!available || !tool.id || busy || toolsLocked}
                          onCheckedChange={(checked) => onToggleTool(tool, checked)}
                        />
                      </div>
                    );
                  })
                )}
              </div>
            </>
          )}
        </CardContent>
      )}
    </Card>
  );
}

// =========================================================================
// Painel: grid de cards, dados de /servers + /oauth/providers +
// /agent/{id}/connections + /agent/{id}/tools. Reusado por ToolsSection
// (visão única de curadoria) e MCPSection.
// =========================================================================

interface McpServersPanelProps {
  agentId: string;
  companyId: string;
  // Notifica o pai quando a curadoria de tools muda (toggle ON/OFF),
  // para que a lista de variáveis do prompt seja recarregada.
  onToolsChanged?: () => void;
}

export function McpServersPanel({ agentId, companyId, onToolsChanged }: McpServersPanelProps) {
  const [loading, setLoading] = useState(true);
  const [servers, setServers] = useState<MCPServer[]>([]);
  const [providers, setProviders] = useState<Record<string, OAuthProviderInfo>>({});
  const [connections, setConnections] = useState<MCPConnection[]>([]);
  const [tools, setTools] = useState<MCPAgentTool[]>([]);
  const [toolsError, setToolsError] = useState(false);
  const [busyServerId, setBusyServerId] = useState<string | null>(null);
  // Intervalo que vigia o fechamento do popup OAuth (fallback quando o
  // postMessage do callback não chega — popup cross-origin sem window.opener).
  const oauthPollRef = useRef<number | null>(null);

  const { toast } = useToast();

  const stopOAuthPoll = useCallback(() => {
    if (oauthPollRef.current !== null) {
      window.clearInterval(oauthPollRef.current);
      oauthPollRef.current = null;
    }
  }, []);

  // Limpa o intervalo se o componente desmontar com um popup aberto.
  useEffect(() => stopOAuthPoll, [stopOAuthPoll]);

  const loadData = useCallback(async () => {
    setToolsError(false);
    try {
      const [serversRes, providersRes, connectionsRes, toolsRes] = await Promise.all([
        fetch(`${MCP_API}/servers`),
        fetch(`${MCP_API}/oauth/providers`),
        fetch(`${MCP_API}/agent/${agentId}/connections?company_id=${companyId}`),
        fetch(`${MCP_API}/agent/${agentId}/tools/catalog?company_id=${companyId}`),
      ]);

      const serversData = serversRes.ok ? await serversRes.json() : {};
      const providersData = providersRes.ok ? await providersRes.json() : {};
      const connectionsData = connectionsRes.ok ? await connectionsRes.json() : {};
      const toolsData = toolsRes.ok ? await toolsRes.json() : {};

      setServers(serversData.servers || []);
      setProviders(providersData.providers || {});
      setConnections(connectionsData.connections || []);
      setTools(toolsData.tools || []);

      // O backend devolve HTTP 500 explícito em falha de leitura do catálogo,
      // o que permite distinguir um erro real de um catálogo genuinamente vazio.
      if (!toolsRes.ok) {
        setToolsError(true);
      }
    } catch (error) {
      // Falha de rede/timeout também caracteriza erro de carregamento do catálogo.
      console.error('Error loading MCP data:', error);
      setToolsError(true);
      toast({
        title: 'Erro',
        description: 'Falha ao carregar servidores MCP',
        variant: 'destructive',
      });
    } finally {
      setLoading(false);
    }
  }, [agentId, companyId, toast]);

  // Refaz o carregamento do catálogo a partir do estado de erro dedicado,
  // sinalizando ao usuário via toast que uma nova tentativa está em curso.
  const handleRetryLoad = useCallback(() => {
    toast({
      title: 'Recarregando',
      description: 'Buscando o catálogo de tools novamente...',
    });
    loadData();
  }, [toast, loadData]);

  // Listener para mensagens do popup OAuth (mesmo mecanismo do fluxo atual)
  useEffect(() => {
    const handleMessage = (event: MessageEvent) => {
      if (event.data?.type === 'MCP_OAUTH_SUCCESS') {
        stopOAuthPoll();
        toast({
          title: 'Conectado',
          description: `${event.data.provider} conectado com sucesso.`,
        });
        setBusyServerId(null);
        loadData();
      }
      if (event.data?.type === 'MCP_OAUTH_ERROR') {
        // Klaviyo: erro de papel insuficiente vira mensagem amigável;
        // demais providers mantêm o erro genérico (SPEC impl §5.3 item 4).
        stopOAuthPoll();
        toast({
          title: 'Erro na conexão',
          description: mapConnectErrorMessage(event.data.provider, String(event.data.error || '')),
          variant: 'destructive',
        });
        setBusyServerId(null);
      }
    };

    window.addEventListener('message', handleMessage);
    return () => window.removeEventListener('message', handleMessage);
  }, [toast, loadData, stopOAuthPoll]);

  useEffect(() => {
    if (agentId) {
      loadData();
    }
  }, [agentId, loadData]);

  const handleConnect = async (server: MCPServer) => {
    if (!server.oauth_provider) return;

    setBusyServerId(server.id);

    try {
      const res = await fetch(
        `${MCP_API}/oauth/url/${server.oauth_provider}?agent_id=${agentId}&mcp_server_id=${server.id}&company_id=${companyId}`,
      );
      const data = await res.json();

      if (!res.ok) {
        throw new Error(data.detail || 'Falha ao gerar URL OAuth');
      }

      // Abrir popup para OAuth
      const width = 600;
      const height = 700;
      const left = (window.innerWidth - width) / 2;
      const top = (window.innerHeight - height) / 2;

      const popup = window.open(
        data.url,
        'MCP OAuth',
        `width=${width},height=${height},left=${left},top=${top}`,
      );

      // Popup bloqueado pelo browser: sem janela não há como concluir o OAuth.
      if (!popup) {
        toast({
          title: 'Pop-up bloqueado',
          description: 'Permita pop-ups para este site e tente conectar novamente.',
          variant: 'destructive',
        });
        setBusyServerId(null);
        return;
      }

      // Fallback de atualização: o callback OAuth posta MCP_OAUTH_SUCCESS, mas
      // em alguns browsers o popup cross-origin (ex.: Sentry/Supabase) perde o
      // window.opener (COOP) e a mensagem nunca chega — o card ficava com
      // spinner até um reload manual. Vigiamos o fechamento do popup e
      // recarregamos os dados, garantindo que as tools apareçam sozinhas.
      stopOAuthPoll();
      const startedAt = Date.now();
      oauthPollRef.current = window.setInterval(() => {
        const timedOut = Date.now() - startedAt > 5 * 60 * 1000;
        if (popup.closed || timedOut) {
          stopOAuthPoll();
          setBusyServerId((current) => (current === server.id ? null : current));
          loadData();
        }
      }, 800);
    } catch (error: any) {
      toast({
        title: 'Erro',
        description: mapConnectErrorMessage(server.oauth_provider, error.message),
        variant: 'destructive',
      });
      setBusyServerId(null);
    }
  };

  const handleDisconnect = async (server: MCPServer) => {
    setBusyServerId(server.id);
    try {
      const res = await fetch(
        `${MCP_API}/agent/${agentId}/disconnect/${server.id}?company_id=${companyId}`,
        { method: 'POST' },
      );

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || 'Falha ao desconectar');
      }

      toast({
        title: 'Desconectado',
        description: `${server.display_name} desconectado`,
      });

      await loadData();
    } catch (error: any) {
      toast({
        title: 'Erro',
        description: error.message,
        variant: 'destructive',
      });
    } finally {
      setBusyServerId(null);
    }
  };

  const handleRefreshTools = async (server: MCPServer) => {
    setBusyServerId(server.id);
    try {
      const res = await fetch(
        `${MCP_API}/agent/${agentId}/refresh-tools/${server.id}?company_id=${companyId}`,
        { method: 'POST' },
      );

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(
          res.status === 404
            ? 'Atualização de tools indisponível no backend.'
            : data.detail || 'Falha ao atualizar tools',
        );
      }

      toast({
        title: 'Tools atualizadas',
        description: `Tools de ${server.display_name} sincronizadas.`,
      });

      await loadData();
    } catch (error: any) {
      toast({
        title: 'Erro',
        description: error.message,
        variant: 'destructive',
      });
    } finally {
      setBusyServerId(null);
    }
  };

  // Config por conexão (ex.: project_ref/read_only do Supabase) via
  // PATCH /agent/{agentId}/connection/{serverId}/config (SPEC impl §4.3).
  const handleSaveConfig = async (server: MCPServer, config: Record<string, unknown>) => {
    try {
      const res = await fetch(
        `${MCP_API}/agent/${agentId}/connection/${server.id}/config?company_id=${companyId}`,
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ connection_config: config }),
        },
      );

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(
          res.status === 404
            ? 'Configuração de conexão indisponível no backend.'
            : data.detail || 'Falha ao salvar configuração',
        );
      }

      toast({
        title: 'Configuração salva',
        description: `Configuração de ${server.display_name} atualizada.`,
      });

      await loadData();
    } catch (error: any) {
      toast({
        title: 'Erro',
        description: error.message,
        variant: 'destructive',
      });
    }
  };

  const handleToggleTool = async (tool: MCPAgentTool, enabled: boolean) => {
    if (!tool.id) return;

    // Update otimista
    const previous = tools;
    setTools((current) =>
      current.map((t) => (t.id === tool.id ? { ...t, is_enabled: enabled } : t)),
    );

    try {
      const res = await fetch(
        `${MCP_API}/agent/${agentId}/tool/${tool.id}/toggle?enabled=${enabled}&company_id=${companyId}`,
        { method: 'PATCH' },
      );

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || 'Falha ao alterar tool');
      }

      // Tool ligada/desligada com sucesso: avisa o pai para recarregar
      // a lista de variáveis do prompt (a tool só aparece lá com is_enabled=true).
      onToolsChanged?.();
    } catch (error: any) {
      setTools(previous);
      toast({
        title: 'Erro',
        description: error.message,
        variant: 'destructive',
      });
    }
  };

  // Liga/desliga TODAS as tools de um servidor numa única chamada de backend
  // (um UPDATE em lote em vez de N PATCHes). Update otimista escopado ao server.
  const handleToggleAll = async (server: MCPServer, enabled: boolean) => {
    setBusyServerId(server.id);

    const previous = tools;
    setTools((current) =>
      current.map((t) => {
        const belongs = t.mcp_server_id === server.id || t.mcp_server_name === server.name;
        if (!belongs) return t;
        // Ao habilitar, não liga tools indisponíveis (alinhado ao backend).
        if (enabled && t.is_available === false) return t;
        return { ...t, is_enabled: enabled };
      }),
    );

    try {
      const res = await fetch(
        `${MCP_API}/agent/${agentId}/tools/toggle-all?company_id=${companyId}`,
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled, mcp_server_id: server.id }),
        },
      );

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || 'Falha ao atualizar tools');
      }

      onToolsChanged?.();
      toast({
        title: enabled ? 'Tools habilitadas' : 'Tools desabilitadas',
        description: `Curadoria de ${server.display_name} atualizada.`,
      });
    } catch (error: any) {
      setTools(previous);
      toast({
        title: 'Erro',
        description: error.message,
        variant: 'destructive',
      });
    } finally {
      setBusyServerId(null);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center p-8">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    );
  }

  if (servers.length === 0) {
    return <p className="text-muted-foreground text-sm">Nenhum servidor MCP disponível</p>;
  }

  const connectionMap = new Map(connections.map((c) => [c.mcp_server_id, c]));

  return (
    <div className="grid grid-cols-1 gap-4">
      {servers.map((server) => {
        const serverTools = tools.filter(
          (t) => t.mcp_server_id === server.id || t.mcp_server_name === server.name,
        );

        return (
          <McpServerCard
            key={server.id}
            server={server}
            connection={connectionMap.get(server.id)}
            tools={serverTools}
            toolsError={toolsError}
            providerInfo={server.oauth_provider ? providers[server.oauth_provider] : undefined}
            busy={busyServerId === server.id}
            onConnect={handleConnect}
            onDisconnect={handleDisconnect}
            onRefreshTools={handleRefreshTools}
            onToggleTool={handleToggleTool}
            onToggleAll={handleToggleAll}
            onSaveConfig={handleSaveConfig}
            onRetryLoad={handleRetryLoad}
          />
        );
      })}
    </div>
  );
}
