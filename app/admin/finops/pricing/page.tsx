'use client';

import { useEffect, useState, Fragment } from 'react';
import { RefreshCw, Search, Edit2, Check, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
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
import { FilterActions, FilterBar, FilterGroup, SearchField } from '@/components/ui/filter-bar';
import {
  PageActions,
  PageDescription,
  PageHeader,
  PageShell,
  PageTitle,
} from '@/components/ui/page-shell';
import { useToast } from '@/hooks/use-toast';

interface PricingItem {
  id: string;
  model_name: string;
  input_price_per_million: number;
  output_price_per_million: number;
  unit: string;
  is_active: boolean;
  provider: string | null;
  display_name: string | null;
  sell_multiplier: number;
}

const PROVIDER_ICONS: Record<string, string> = {
  anthropic: 'AN',
  openai: 'OA',
  google: 'GG',
  openrouter: 'OR',
  other: 'OT',
};

const PROVIDER_ORDER = ['anthropic', 'openai', 'google', 'openrouter', 'other'];

export default function PricingPage() {
  const { toast } = useToast();
  const [pricing, setPricing] = useState<PricingItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [reloading, setReloading] = useState(false);
  const [syncingOpenRouter, setSyncingOpenRouter] = useState(false);
  const [search, setSearch] = useState('');
  const [filterProvider, setFilterProvider] = useState('all');
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState({
    input: 0,
    output: 0,
    sell_multiplier: 2.68,
    is_active: true,
  });
  const [saving, setSaving] = useState(false);
  const [multiplierDialogOpen, setMultiplierDialogOpen] = useState(false);
  const [bulkMultiplier, setBulkMultiplier] = useState('2.68');
  const [updatingMultiplier, setUpdatingMultiplier] = useState(false);

  const fetchPricing = async () => {
    setLoading(true);
    try {
      const response = await fetch('/api/admin/pricing', { credentials: 'include' });
      if (response.ok) {
        const data = await response.json();
        setPricing(data.data || []);
      }
    } catch (error) {
      console.error('Error fetching pricing:', error);
    } finally {
      setLoading(false);
    }
  };

  const reloadCache = async () => {
    setReloading(true);
    try {
      const response = await fetch('/api/admin/pricing/reload-cache', {
        method: 'POST',
        credentials: 'include',
      });
      if (response.ok) {
        const data = await response.json();
        toast({
          title: 'Sucesso',
          description: `Cache atualizado com ${data.count} modelos.`,
        });
      } else {
        toast({
          title: 'Erro',
          description: 'Não foi possível recarregar o cache.',
          variant: 'destructive',
        });
      }
    } catch (error) {
      console.error('Error reloading cache:', error);
      toast({
        title: 'Erro',
        description: 'Não foi possível recarregar o cache.',
        variant: 'destructive',
      });
    } finally {
      setReloading(false);
    }
  };

  const handleEdit = (item: PricingItem) => {
    setEditingId(item.id);
    setEditForm({
      input: item.input_price_per_million,
      output: item.output_price_per_million,
      sell_multiplier: item.sell_multiplier ?? 2.68,
      is_active: item.is_active,
    });
  };

  const handleSave = async () => {
    if (!editingId) return;

    setSaving(true);
    try {
      const response = await fetch(`/api/admin/pricing/${editingId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          input_price_per_million: editForm.input,
          output_price_per_million: editForm.output,
          sell_multiplier: editForm.sell_multiplier,
          is_active: editForm.is_active,
        }),
      });

      if (response.ok) {
        await fetchPricing();
        setEditingId(null);
      } else {
        toast({
          title: 'Erro',
          description: 'Não foi possível salvar o preço.',
          variant: 'destructive',
        });
      }
    } catch (error) {
      console.error('Error saving:', error);
      toast({
        title: 'Erro',
        description: 'Não foi possível salvar o preço.',
        variant: 'destructive',
      });
    } finally {
      setSaving(false);
    }
  };

  const handleCancel = () => {
    setEditingId(null);
  };

  const handleBulkMultiplierUpdate = async () => {
    const value = Number.parseFloat(bulkMultiplier);
    if (!Number.isFinite(value) || value <= 0) {
      toast({
        title: 'Atenção',
        description: 'Informe um multiplicador válido.',
        variant: 'destructive',
      });
      return;
    }

    setUpdatingMultiplier(true);
    try {
      const response = await fetch('/api/admin/pricing/bulk-update-multiplier', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ sell_multiplier: value, provider: 'all' }),
      });

      if (response.ok) {
        toast({
          title: 'Sucesso',
          description: 'Multiplicador atualizado com sucesso.',
        });
        setMultiplierDialogOpen(false);
        await fetchPricing();
      } else {
        toast({
          title: 'Erro',
          description: 'Não foi possível atualizar o multiplicador.',
          variant: 'destructive',
        });
      }
    } catch (error) {
      console.error('Error updating multiplier:', error);
      toast({
        title: 'Erro',
        description: 'Não foi possível atualizar o multiplicador.',
        variant: 'destructive',
      });
    } finally {
      setUpdatingMultiplier(false);
    }
  };

  const handleSyncOpenRouter = async () => {
    setSyncingOpenRouter(true);
    try {
      const response = await fetch('/api/admin/pricing/sync-openrouter', {
        method: 'POST',
        credentials: 'include',
      });

      if (response.ok) {
        toast({
          title: 'Sucesso',
          description: 'Sincronização concluída.',
        });
        await fetchPricing();
      } else {
        toast({
          title: 'Erro',
          description: 'Não foi possível sincronizar os modelos.',
          variant: 'destructive',
        });
      }
    } catch (error) {
      console.error('Error syncing OpenRouter:', error);
      toast({
        title: 'Erro',
        description: 'Não foi possível sincronizar os modelos.',
        variant: 'destructive',
      });
    } finally {
      setSyncingOpenRouter(false);
    }
  };

  useEffect(() => {
    fetchPricing();
  }, []);

  // Filter and group by provider
  const filteredPricing = pricing.filter((item) => {
    const matchesSearch = item.model_name.toLowerCase().includes(search.toLowerCase());
    const matchesProvider = filterProvider === 'all' || item.provider === filterProvider;
    return matchesSearch && matchesProvider;
  });

  const groupedPricing = PROVIDER_ORDER.reduce(
    (acc, provider) => {
      const items = filteredPricing.filter((p) => (p.provider || 'other') === provider);
      if (items.length > 0) {
        acc[provider] = items;
      }
      return acc;
    },
    {} as Record<string, PricingItem[]>,
  );

  const formatPrice = (value: number) => {
    return `$ ${value.toFixed(4)}`;
  };

  return (
    <PageShell>
      <PageHeader>
        <div>
          <PageTitle>Tabela de Custos LLM</PageTitle>
          <PageDescription>Preços por milhão de tokens (input/output)</PageDescription>
        </div>

        <PageActions>
          <Button
            onClick={() => setMultiplierDialogOpen(true)}
            variant="outline"
            className="border-border text-foreground hover:bg-muted"
          >
            Atualizar Multiplicador
          </Button>

          <Button
            onClick={handleSyncOpenRouter}
            disabled={syncingOpenRouter}
            className="bg-primary hover:bg-primary/90 text-primary-foreground"
          >
            <RefreshCw className={`w-4 h-4 mr-2 ${syncingOpenRouter ? 'animate-spin' : ''}`} />
            {syncingOpenRouter ? 'Sincronizando...' : 'Sync OpenRouter'}
          </Button>

          <Button
            onClick={reloadCache}
            disabled={reloading}
            className="bg-primary hover:bg-primary/90 text-primary-foreground"
          >
            <RefreshCw className={`w-4 h-4 mr-2 ${reloading ? 'animate-spin' : ''}`} />
            Reload Cache
          </Button>
        </PageActions>
      </PageHeader>

      <Dialog open={multiplierDialogOpen} onOpenChange={setMultiplierDialogOpen}>
        <DialogContent className="bg-card border-border text-foreground">
          <DialogHeader>
            <DialogTitle>Atualizar multiplicador</DialogTitle>
            <DialogDescription>
              Informe o multiplicador aplicado a todos os modelos ativos.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="bulk-multiplier">Multiplicador</Label>
            <Input
              id="bulk-multiplier"
              type="number"
              min="0.01"
              step="0.01"
              value={bulkMultiplier}
              onChange={(event) => setBulkMultiplier(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault();
                  void handleBulkMultiplierUpdate();
                }
              }}
            />
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setMultiplierDialogOpen(false)}
              disabled={updatingMultiplier}
            >
              Cancelar
            </Button>
            <Button onClick={handleBulkMultiplierUpdate} disabled={updatingMultiplier}>
              {updatingMultiplier ? 'Atualizando...' : 'Atualizar'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Filters */}
      <FilterBar>
        <FilterGroup>
          <SearchField
            icon={Search}
            placeholder="Buscar modelo..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            wrapperClassName="max-w-md"
          />
        </FilterGroup>
        <FilterActions>
          <Select value={filterProvider} onValueChange={setFilterProvider}>
            <SelectTrigger className="w-[180px]">
              <SelectValue placeholder="Filtrar provider" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Todos</SelectItem>
              <SelectItem value="anthropic">Anthropic</SelectItem>
              <SelectItem value="openai">OpenAI</SelectItem>
              <SelectItem value="google">Google</SelectItem>
              <SelectItem value="openrouter">OpenRouter</SelectItem>
              <SelectItem value="other">Outros</SelectItem>
            </SelectContent>
          </Select>
        </FilterActions>
      </FilterBar>

      {/* Table */}
      <div className="bg-card border border-border rounded-xl overflow-hidden">
        {loading ? (
          <div className="p-8 text-center text-muted-foreground">Carregando...</div>
        ) : (
          <table className="w-full">
            <thead className="bg-muted">
              <tr>
                <th className="px-6 py-4 text-left text-xs font-medium text-muted-foreground uppercase">
                  Modelo
                </th>
                <th className="px-6 py-4 text-right text-xs font-medium text-muted-foreground uppercase">
                  Input/1M
                </th>
                <th className="px-6 py-4 text-right text-xs font-medium text-muted-foreground uppercase">
                  Output/1M
                </th>
                <th className="px-6 py-4 text-right text-xs font-medium text-muted-foreground uppercase">
                  Multiplicador
                </th>
                <th className="px-6 py-4 text-center text-xs font-medium text-muted-foreground uppercase">
                  Ativo
                </th>
                <th className="px-6 py-4 text-center text-xs font-medium text-muted-foreground uppercase">
                  Ações
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {Object.entries(groupedPricing).map(([provider, items]) => (
                <Fragment key={provider}>
                  {/* Provider Header */}
                  <tr className="bg-muted/50">
                    <td colSpan={6} className="px-6 py-3">
                      <span className="text-lg font-semibold text-foreground">
                        <span className="mr-2 rounded border border-border bg-background px-1.5 py-0.5 text-xs text-muted-foreground">
                          {PROVIDER_ICONS[provider]}
                        </span>
                        {provider.charAt(0).toUpperCase() + provider.slice(1)}
                      </span>
                    </td>
                  </tr>

                  {/* Provider Items */}
                  {items.map((item) => (
                    <tr key={item.id} className="hover:bg-accent/50">
                      <td className="px-6 py-4 text-foreground font-mono text-sm">
                        {item.model_name}
                      </td>
                      <td className="px-6 py-4 text-right">
                        {editingId === item.id ? (
                          <Input
                            type="number"
                            step="0.0001"
                            value={editForm.input}
                            onChange={(e) =>
                              setEditForm({ ...editForm, input: parseFloat(e.target.value) })
                            }
                            className="w-24 bg-background border-primary text-foreground text-right"
                          />
                        ) : (
                          <span
                            className={`text-foreground ${item.unit === 'minute' ? 'text-primary' : ''}`}
                          >
                            {formatPrice(item.input_price_per_million)}
                            {item.unit === 'minute' && '/min'}
                          </span>
                        )}
                      </td>
                      <td className="px-6 py-4 text-right">
                        {editingId === item.id ? (
                          <Input
                            type="number"
                            step="0.0001"
                            value={editForm.output}
                            onChange={(e) =>
                              setEditForm({ ...editForm, output: parseFloat(e.target.value) })
                            }
                            className="w-24 bg-background border-primary text-foreground text-right"
                          />
                        ) : (
                          <span className="text-foreground">
                            {formatPrice(item.output_price_per_million)}
                          </span>
                        )}
                      </td>
                      <td className="px-6 py-4 text-right">
                        {editingId === item.id ? (
                          <Input
                            type="number"
                            step="0.01"
                            value={editForm.sell_multiplier}
                            onChange={(e) =>
                              setEditForm({
                                ...editForm,
                                sell_multiplier: parseFloat(e.target.value),
                              })
                            }
                            className="w-20 bg-background border-primary text-foreground text-right"
                          />
                        ) : (
                          <span className="text-success font-medium">
                            {(item.sell_multiplier ?? 2.68).toFixed(2)}x
                          </span>
                        )}
                      </td>
                      <td className="px-6 py-4 text-center">
                        {editingId === item.id ? (
                          <div className="flex items-center justify-center">
                            <Switch
                              checked={editForm.is_active}
                              onCheckedChange={(checked) =>
                                setEditForm({ ...editForm, is_active: checked })
                              }
                            />
                          </div>
                        ) : (
                          <span
                            className={`px-2 py-1 rounded text-xs font-medium ${item.is_active ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'}`}
                          >
                            {item.is_active ? 'Ativo' : 'Inativo'}
                          </span>
                        )}
                      </td>
                      <td className="px-6 py-4 text-center">
                        {editingId === item.id ? (
                          <div className="flex gap-2 justify-center">
                            <Button
                              size="sm"
                              onClick={handleSave}
                              disabled={saving}
                              className="bg-primary hover:bg-primary/90"
                            >
                              <Check className="w-4 h-4" />
                            </Button>
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={handleCancel}
                              className="bg-background text-foreground hover:bg-muted border-0"
                            >
                              <X className="w-4 h-4" />
                            </Button>
                          </div>
                        ) : (
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() => handleEdit(item)}
                            className="text-muted-foreground hover:text-foreground"
                          >
                            <Edit2 className="w-4 h-4" />
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </Fragment>
              ))}

              {filteredPricing.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-6 py-8 text-center text-muted-foreground">
                    {pricing.length === 0
                      ? 'Nenhum modelo encontrado. Execute o seed_pricing.py primeiro.'
                      : 'Nenhum modelo corresponde aos filtros.'}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>

      {/* Info */}
      <div className="mt-6 p-4 bg-brand-muted border border-primary/20 rounded-lg">
        <p className="text-primary text-sm">
          <strong>Dica:</strong> Após editar preços, clique em "Reload Cache" para que as mudanças
          tenham efeito imediato no cálculo de custos.
        </p>
      </div>
    </PageShell>
  );
}
