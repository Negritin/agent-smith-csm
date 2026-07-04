'use client';

import React, { useState, useEffect } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import {
  Loader2,
  ShoppingBag,
  Store,
  RefreshCw,
  Link,
  Unlink,
  Search,
  CheckCircle,
  AlertCircle,
  Globe,
  Zap,
} from 'lucide-react';
import { useToast } from '@/hooks/use-toast';

interface UCPCapability {
  name: string;
  tool_name: string;
  version: string;
  is_extension: boolean;
}

interface UCPConnection {
  id: string;
  store_url: string;
  manifest_version?: string;
  preferred_transport: string;
  capabilities: string[];
  is_active: boolean;
  last_used_at?: string;
  created_at: string;
}

interface DiscoveryResult {
  success: boolean;
  store_url: string;
  manifest_version?: string;
  capabilities: UCPCapability[];
  preferred_transport?: string;
  error?: string;
}

interface Props {
  agentId: string;
  companyId: string;
}

export function UCPConfigTab({ agentId, companyId }: Props) {
  const [loading, setLoading] = useState(true);
  const [connections, setConnections] = useState<UCPConnection[]>([]);
  const [storeUrl, setStoreUrl] = useState('');
  const [discovering, setDiscovering] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [disconnecting, setDisconnecting] = useState<string | null>(null);
  const [discoveryResult, setDiscoveryResult] = useState<DiscoveryResult | null>(null);

  const { toast } = useToast();
  // F02: todas as chamadas UCP passam pelo proxy admin autenticado
  // (/api/admin/proxy/ucp/...), que injeta a sessão admin + X-Admin-API-Key e
  // o JWT de tenant. Nunca falar com o backend via NEXT_PUBLIC_BACKEND_URL.
  const UCP_PROXY = '/api/admin/proxy/ucp';

  useEffect(() => {
    if (agentId) {
      loadConnections();
    }
  }, [agentId]);

  const loadConnections = async () => {
    setLoading(true);
    try {
      const res = await fetch(`${UCP_PROXY}/connections/${agentId}`);
      const data = await res.json();
      setConnections(data.connections || []);
    } catch (error) {
      console.error('Error loading UCP connections:', error);
      toast({
        title: 'Erro',
        description: 'Falha ao carregar conexões UCP',
        variant: 'destructive',
      });
    } finally {
      setLoading(false);
    }
  };

  const handleDiscover = async () => {
    if (!storeUrl.trim()) {
      toast({
        title: 'Erro',
        description: 'Digite a URL da loja',
        variant: 'destructive',
      });
      return;
    }

    setDiscovering(true);
    setDiscoveryResult(null);

    try {
      const res = await fetch(`${UCP_PROXY}/discover`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // companyId é lido apenas pelo proxy para resolver o tenant do JWT;
        // o backend (DiscoverRequest) ignora campos extras.
        body: JSON.stringify({ store_url: storeUrl.trim(), companyId }),
      });

      const data = await res.json();
      setDiscoveryResult(data);

      if (data.success) {
        toast({
          title: 'Loja UCP encontrada',
          description: `${data.capabilities.length} capabilities disponíveis`,
        });
      } else {
        toast({
          title: 'Loja não compatível',
          description: data.error || 'Loja não possui manifest UCP',
          variant: 'destructive',
        });
      }
    } catch (error: any) {
      toast({
        title: 'Erro',
        description: error.message || 'Falha ao descobrir loja',
        variant: 'destructive',
      });
    } finally {
      setDiscovering(false);
    }
  };

  const handleConnect = async () => {
    if (!discoveryResult?.success) {
      return;
    }

    setConnecting(true);
    try {
      const res = await fetch(`${UCP_PROXY}/connect`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          agent_id: agentId,
          company_id: companyId,
          store_url: storeUrl.trim(),
        }),
      });

      const data = await res.json();

      if (data.success) {
        toast({
          title: 'Loja conectada',
          description: `${data.capabilities?.length || 0} tools disponíveis para o agente`,
        });
        setStoreUrl('');
        setDiscoveryResult(null);
        await loadConnections();
      } else {
        toast({
          title: 'Erro',
          description: data.error || 'Falha ao conectar loja',
          variant: 'destructive',
        });
      }
    } catch (error: any) {
      toast({
        title: 'Erro',
        description: error.message,
        variant: 'destructive',
      });
    } finally {
      setConnecting(false);
    }
  };

  const handleDisconnect = async (connectionId: string) => {
    setDisconnecting(connectionId);
    try {
      const res = await fetch(`${UCP_PROXY}/disconnect/${connectionId}`, {
        method: 'DELETE',
      });

      if (res.ok) {
        toast({
          title: 'Desconectado',
          description: 'Loja desconectada com sucesso',
        });
        await loadConnections();
      } else {
        const data = await res.json();
        throw new Error(data.detail || 'Falha ao desconectar');
      }
    } catch (error: any) {
      toast({
        title: 'Erro',
        description: error.message,
        variant: 'destructive',
      });
    } finally {
      setDisconnecting(null);
    }
  };

  const handleRefresh = async (connectionId: string) => {
    try {
      const res = await fetch(`${UCP_PROXY}/refresh/${connectionId}`, {
        method: 'POST',
      });

      const data = await res.json();

      if (data.success) {
        toast({
          title: 'Atualizado',
          description: 'Manifest atualizado com sucesso',
        });
        await loadConnections();
      } else {
        toast({
          title: 'Erro',
          description: data.error || 'Falha ao atualizar',
          variant: 'destructive',
        });
      }
    } catch (error: any) {
      toast({
        title: 'Erro',
        description: error.message,
        variant: 'destructive',
      });
    }
  };

  const getTransportIcon = (transport: string) => {
    switch (transport) {
      case 'mcp':
        return <Zap className="h-3 w-3" />;
      case 'a2a':
        return <Globe className="h-3 w-3" />;
      default:
        return null;
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center p-8">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div className="space-y-6 mt-6">
      {/* Info Card */}
      <Card className="bg-primary border-transparent">
        <CardContent className="p-4">
          <div className="flex items-start gap-3">
            <ShoppingBag className="h-5 w-5 text-primary-foreground mt-0.5" />
            <div>
              <p className="text-sm text-primary-foreground">
                <strong>UCP (Universal Commerce Protocol)</strong> permite integrar lojas de
                e-commerce.
              </p>
              <p className="text-xs text-primary-foreground/90 mt-1">
                Conecte qualquer loja que publique{' '}
                <code className="bg-card/20 px-1 rounded">/.well-known/ucp</code>
              </p>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Conexões Ativas */}
      {connections.length > 0 && (
        <Card className="bg-card border-border">
          <CardHeader className="flex flex-row items-center justify-between">
            <CardTitle className="text-sm text-foreground flex items-center gap-2">
              <CheckCircle className="h-4 w-4 text-primary" />
              Lojas Conectadas
            </CardTitle>
            <Button
              variant="ghost"
              size="sm"
              onClick={loadConnections}
              className="text-muted-foreground hover:text-foreground"
            >
              <RefreshCw className="h-4 w-4" />
            </Button>
          </CardHeader>
          <CardContent className="space-y-3">
            {connections.map((conn) => (
              <div
                key={conn.id}
                className="flex items-center justify-between p-4 bg-primary border-transparent rounded-lg"
              >
                <div className="flex items-center gap-3">
                  <Store className="h-5 w-5 text-primary-foreground" />
                  <div>
                    <p className="font-medium text-primary-foreground">{conn.store_url}</p>
                    <div className="flex items-center gap-2 mt-1">
                      <span className="text-xs text-primary-foreground/80">
                        v{conn.manifest_version}
                      </span>
                      <Badge
                        variant="outline"
                        className="text-xs border-primary-foreground/30 text-primary-foreground/90"
                      >
                        {getTransportIcon(conn.preferred_transport)}
                        <span className="ml-1">{conn.preferred_transport.toUpperCase()}</span>
                      </Badge>
                      <span className="text-xs text-primary-foreground/80">
                        {conn.capabilities.length} capabilities
                      </span>
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <Badge className="bg-card text-primary hover:bg-card/90 text-xs">Ativo</Badge>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => handleRefresh(conn.id)}
                    className="text-primary-foreground/70 hover:text-foreground hover:bg-card/20"
                    title="Atualizar manifest"
                  >
                    <RefreshCw className="h-4 w-4" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => handleDisconnect(conn.id)}
                    disabled={disconnecting === conn.id}
                    className="text-primary-foreground/70 hover:text-foreground hover:bg-danger/20"
                  >
                    {disconnecting === conn.id ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      <Unlink className="h-4 w-4" />
                    )}
                  </Button>
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      {/* Adicionar Nova Conexão */}
      <Card className="bg-card border-border">
        <CardHeader>
          <CardTitle className="text-sm text-foreground flex items-center gap-2">
            <Globe className="h-4 w-4 text-primary" />
            Conectar Loja UCP
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <label className="text-xs text-muted-foreground">URL da Loja</label>
            <div className="flex gap-2">
              <Input
                placeholder="https://minhaloja.com.br"
                value={storeUrl}
                onChange={(e) => {
                  setStoreUrl(e.target.value);
                  setDiscoveryResult(null);
                }}
                className="bg-background border-border text-foreground"
              />
              <Button
                onClick={handleDiscover}
                disabled={discovering || !storeUrl.trim()}
                className="whitespace-nowrap"
              >
                {discovering ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <>
                    <Search className="h-4 w-4 mr-2" />
                    Descobrir
                  </>
                )}
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              Informe a URL da loja. O Smith buscará{' '}
              <code className="bg-muted px-1 rounded">/.well-known/ucp</code> automaticamente.
            </p>
          </div>

          {/* Resultado do Discovery */}
          {discoveryResult && (
            <div
              className={`p-4 rounded-lg border ${
                discoveryResult.success
                  ? 'bg-success/10 border-success/25'
                  : 'bg-danger/10 border-danger/25'
              }`}
            >
              {discoveryResult.success ? (
                <>
                  <div className="flex items-center justify-between mb-3">
                    <div className="flex items-center gap-2">
                      <CheckCircle className="h-5 w-5 text-success" />
                      <span className="text-success font-medium">Loja UCP Encontrada!</span>
                    </div>
                    <Badge variant="outline" className="border-success/40 text-success">
                      v{discoveryResult.manifest_version}
                    </Badge>
                  </div>

                  <div className="mb-3">
                    <p className="text-xs text-muted-foreground mb-2">Capabilities disponíveis:</p>
                    <div className="flex flex-wrap gap-1">
                      {discoveryResult.capabilities.map((cap) => (
                        <Badge
                          key={cap.name}
                          variant="secondary"
                          className="text-xs bg-muted text-muted-foreground"
                        >
                          {cap.tool_name}
                        </Badge>
                      ))}
                    </div>
                  </div>

                  <div className="flex items-center justify-between">
                    <span className="text-xs text-muted-foreground">
                      Transport: {discoveryResult.preferred_transport?.toUpperCase()}
                    </span>
                    <Button
                      onClick={handleConnect}
                      disabled={connecting}
                      className="bg-success hover:bg-success/90 text-primary-foreground"
                    >
                      {connecting ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        <>
                          <Link className="h-4 w-4 mr-2" />
                          Conectar
                        </>
                      )}
                    </Button>
                  </div>
                </>
              ) : (
                <div className="flex items-center gap-2">
                  <AlertCircle className="h-5 w-5 text-danger" />
                  <span className="text-danger">{discoveryResult.error}</span>
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Tools Disponíveis */}
      {connections.length > 0 && (
        <Card className="bg-card border-border">
          <CardHeader>
            <CardTitle className="text-sm text-foreground">Variáveis para Prompt</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-xs text-muted-foreground mb-3">
              Use estas variáveis no prompt do agente:
            </p>
            <div className="grid grid-cols-1 gap-2">
              {/* Capabilities do manifest (checkout, fulfillment) */}
              {connections.flatMap((conn) =>
                conn.capabilities.map((cap) => {
                  const toolName = cap.split('.').slice(2).join('_');
                  return (
                    <div
                      key={`${conn.id}-${cap}`}
                      className="flex items-center justify-between p-3 bg-background rounded border border-border"
                    >
                      <div>
                        <code className="text-success text-sm font-mono">
                          {`{{ucp_${toolName}}}`}
                        </code>
                        <p className="text-xs text-muted-foreground mt-1">{cap}</p>
                      </div>
                      <Badge
                        variant="outline"
                        className="text-xs border-border text-muted-foreground"
                      >
                        {conn.store_url.replace('https://', '')}
                      </Badge>
                    </div>
                  );
                }),
              )}

              {/* Storefront MCP Tools (busca de produtos e políticas) */}
              {connections.map((conn) => (
                <React.Fragment key={conn.id}>
                  <div className="flex items-center justify-between p-3 bg-background rounded border border-border">
                    <div>
                      <code className="text-success text-sm font-mono">
                        {`{{store_product_search}}`}
                      </code>
                      <p className="text-xs text-muted-foreground mt-1">
                        Busca produtos na loja (Storefront MCP)
                      </p>
                    </div>
                    <Badge
                      variant="outline"
                      className="text-xs border-border text-muted-foreground"
                    >
                      {conn.store_url.replace('https://', '')}
                    </Badge>
                  </div>
                  <div className="flex items-center justify-between p-3 bg-background rounded border border-border">
                    <div>
                      <code className="text-success text-sm font-mono">
                        {`{{store_policy_search}}`}
                      </code>
                      <p className="text-xs text-muted-foreground mt-1">
                        Perguntas sobre políticas/FAQ da loja
                      </p>
                    </div>
                    <Badge
                      variant="outline"
                      className="text-xs border-border text-muted-foreground"
                    >
                      {conn.store_url.replace('https://', '')}
                    </Badge>
                  </div>
                </React.Fragment>
              ))}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
