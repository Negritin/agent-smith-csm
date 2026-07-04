'use client';

import { useEffect, useState } from 'react';
import { Plus, Edit2, X, Trash2, Check } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import {
  PageActions,
  PageDescription,
  PageHeader,
  PageShell,
  PageTitle,
} from '@/components/ui/page-shell';
import { StatusPill } from '@/components/ui/status-pill';
import { useToast } from '@/hooks/use-toast';

// Custo médio por mensagem em BRL (gpt-4o-mini com multiplicador ~2.7)
const CUSTO_MEDIO_MSG_BRL = 0.02;

function calcularMensagensEstimadas(priceBrl: number): number {
  return Math.round(priceBrl / CUSTO_MEDIO_MSG_BRL);
}

interface PlanFeature {
  name: string;
  included: boolean;
}

interface Plan {
  id: string;
  name: string;
  description: string | null;
  price_brl: number;
  display_credits: number;
  max_agents: number;
  max_knowledge_bases: number;
  max_users: number;
  features: PlanFeature[] | string[];
  stripe_product_id: string | null;
  stripe_price_id: string | null;
  is_active: boolean;
  display_order: number;
}

interface PlanFormData {
  name: string;
  description: string;
  price_brl: number;
  display_credits: number;
  features: PlanFeature[];
  is_active: boolean;
  display_order: number;
  stripe_product_id: string;
  stripe_price_id: string;
}

const defaultFormData: PlanFormData = {
  name: '',
  description: '',
  price_brl: 399,
  display_credits: 15000,
  features: [
    { name: '3 Agentes', included: true },
    { name: '5 Bases de Conhecimento', included: true },
    { name: '5 Usuários', included: true },
  ],
  is_active: true,
  display_order: 0,
  stripe_product_id: '',
  stripe_price_id: '',
};

// Helper para normalizar features (pode vir como string[] ou PlanFeature[])
function normalizeFeatures(features: PlanFeature[] | string[] | null | undefined): PlanFeature[] {
  if (!features || !Array.isArray(features)) return [];

  // Se já é PlanFeature[]
  if (features.length > 0 && typeof features[0] === 'object') {
    return features as PlanFeature[];
  }

  // Se é string[], converte para PlanFeature[]
  return (features as string[]).map((name) => ({ name, included: true }));
}

export default function PlansPage() {
  const { toast } = useToast();
  const [plans, setPlans] = useState<Plan[]>([]);
  const [loading, setLoading] = useState(true);
  const [showModal, setShowModal] = useState(false);
  const [editingPlan, setEditingPlan] = useState<Plan | null>(null);
  const [formData, setFormData] = useState<PlanFormData>(defaultFormData);
  const [saving, setSaving] = useState(false);
  const [showInactive, setShowInactive] = useState(false);
  const [newFeatureName, setNewFeatureName] = useState('');
  const [deletePlanId, setDeletePlanId] = useState<string | null>(null);

  const fetchPlans = async () => {
    setLoading(true);
    try {
      const url = showInactive ? '/api/admin/plans?include_inactive=true' : '/api/admin/plans';
      const response = await fetch(url, { credentials: 'include' });
      if (response.ok) {
        const data = await response.json();
        setPlans(data.data || []);
      }
    } catch (error) {
      console.error('Error fetching plans:', error);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchPlans();
  }, [showInactive]);

  const handleCreate = () => {
    setEditingPlan(null);
    setFormData(defaultFormData);
    setNewFeatureName('');
    setShowModal(true);
  };

  const handleEdit = (plan: Plan) => {
    setEditingPlan(plan);
    setFormData({
      name: plan.name,
      description: plan.description || '',
      price_brl: plan.price_brl,
      display_credits: plan.display_credits,
      features: normalizeFeatures(plan.features),
      is_active: plan.is_active,
      display_order: plan.display_order,
      stripe_product_id: plan.stripe_product_id || '',
      stripe_price_id: plan.stripe_price_id || '',
    });
    setNewFeatureName('');
    setShowModal(true);
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const url = editingPlan ? `/api/admin/plans/${editingPlan.id}` : '/api/admin/plans';

      const response = await fetch(url, {
        method: editingPlan ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify(formData),
      });

      if (response.ok) {
        await fetchPlans();
        setShowModal(false);
      } else {
        toast({
          title: 'Erro',
          description: 'Não foi possível salvar o plano.',
          variant: 'destructive',
        });
      }
    } catch (error) {
      console.error('Error saving plan:', error);
      toast({
        title: 'Erro',
        description: 'Não foi possível salvar o plano.',
        variant: 'destructive',
      });
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (planId: string) => {
    try {
      const response = await fetch(`/api/admin/plans/${planId}`, {
        method: 'DELETE',
        credentials: 'include',
      });

      if (response.ok) {
        await fetchPlans();
      } else {
        toast({
          title: 'Erro',
          description: 'Não foi possível desativar o plano.',
          variant: 'destructive',
        });
      }
    } catch (error) {
      console.error('Error deleting plan:', error);
      toast({
        title: 'Erro',
        description: 'Não foi possível desativar o plano.',
        variant: 'destructive',
      });
    }
  };

  // Feature management
  const addFeature = () => {
    if (!newFeatureName.trim()) return;
    setFormData({
      ...formData,
      features: [...formData.features, { name: newFeatureName.trim(), included: true }],
    });
    setNewFeatureName('');
  };

  const removeFeature = (index: number) => {
    setFormData({
      ...formData,
      features: formData.features.filter((_, i) => i !== index),
    });
  };

  const toggleFeatureIncluded = (index: number) => {
    const newFeatures = [...formData.features];
    newFeatures[index] = { ...newFeatures[index], included: !newFeatures[index].included };
    setFormData({ ...formData, features: newFeatures });
  };

  const updateFeatureName = (index: number, name: string) => {
    const newFeatures = [...formData.features];
    newFeatures[index] = { ...newFeatures[index], name };
    setFormData({ ...formData, features: newFeatures });
  };

  const formatCurrency = (value: number) => {
    return new Intl.NumberFormat('pt-BR', {
      style: 'currency',
      currency: 'BRL',
    }).format(value);
  };

  const formatNumber = (value: number) => {
    return new Intl.NumberFormat('pt-BR').format(value);
  };

  return (
    <PageShell>
      <PageHeader>
        <div>
          <PageTitle>Planos de Assinatura</PageTitle>
          <PageDescription>Configure os planos disponíveis para os clientes</PageDescription>
        </div>

        <PageActions className="gap-4">
          <label className="flex items-center gap-2 text-muted-foreground text-sm">
            <Switch checked={showInactive} onCheckedChange={setShowInactive} />
            Mostrar inativos
          </label>
          <Button onClick={handleCreate}>
            <Plus className="w-4 h-4 mr-2" />
            Novo Plano
          </Button>
        </PageActions>
      </PageHeader>

      {/* Plans Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-6">
        {loading ? (
          <div className="col-span-full text-center text-muted-foreground py-12">Carregando...</div>
        ) : plans.length === 0 ? (
          <div className="col-span-full text-center text-muted-foreground py-12">
            Nenhum plano encontrado. Crie o primeiro clicando em &quot;Novo Plano&quot;.
          </div>
        ) : (
          plans.map((plan) => {
            const features = normalizeFeatures(plan.features);
            const mensagensEstimadas = calcularMensagensEstimadas(plan.price_brl);

            return (
              <div
                key={plan.id}
                className={`bg-card dark:bg-card border rounded-xl p-6 ${
                  plan.is_active ? 'border-border' : 'border-destructive/30 opacity-60'
                }`}
              >
                {/* Plan Header */}
                <div className="flex justify-between items-start mb-4">
                  <div>
                    <h3 className="text-xl font-bold text-card-foreground">{plan.name}</h3>
                    {plan.description && (
                      <p className="text-muted-foreground text-sm mt-1">{plan.description}</p>
                    )}
                  </div>
                  <div className="flex gap-1">
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => handleEdit(plan)}
                      className="text-muted-foreground hover:text-foreground"
                    >
                      <Edit2 className="w-4 h-4" />
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => setDeletePlanId(plan.id)}
                      className="text-muted-foreground hover:text-destructive"
                    >
                      <Trash2 className="w-4 h-4" />
                    </Button>
                  </div>
                </div>

                {/* Price */}
                <div className="mb-4">
                  <span className="text-3xl font-bold text-card-foreground">
                    {formatCurrency(plan.price_brl)}
                  </span>
                  <span className="text-muted-foreground text-sm">/mês</span>
                </div>

                {/* Credits & Messages */}
                <div className="bg-primary border border-primary rounded-lg px-4 py-3 mb-4">
                  <p className="text-primary-foreground text-sm font-medium">
                    {formatNumber(plan.display_credits)} créditos
                  </p>
                  <p className="text-primary-foreground text-sm">
                    ~{formatNumber(mensagensEstimadas)} mensagens*
                  </p>
                </div>

                {/* Dynamic Features */}
                {features.length > 0 && (
                  <div className="space-y-2 text-sm border-t border-border pt-4 mb-4">
                    {features.map((feature, idx) => (
                      <div key={idx} className="flex items-center gap-2">
                        {feature.included && (
                          <div className="bg-foreground rounded-full p-0.5 flex items-center justify-center">
                            <Check className="w-3 h-3 text-primary-foreground" strokeWidth={3} />
                          </div>
                        )}
                        <span
                          className={feature.included ? 'text-foreground' : 'text-muted-foreground'}
                        >
                          {feature.name}
                        </span>
                      </div>
                    ))}
                  </div>
                )}

                {/* Footnote */}
                <p className="text-muted-foreground text-xs mb-4">*estimativa com modelo padrão</p>

                {/* Stripe Status */}
                <div className="pt-4 border-t border-border">
                  <div className="flex items-center gap-2">
                    {plan.stripe_price_id ? (
                      <StatusPill tone="brand">Stripe</StatusPill>
                    ) : (
                      <StatusPill tone="warning">Sem Stripe</StatusPill>
                    )}
                    {!plan.is_active && <StatusPill tone="danger">Inativo</StatusPill>}
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>

      {/* Modal */}
      {showModal && (
        <div className="fixed inset-0 bg-foreground/40 flex items-center justify-center z-50">
          <div className="bg-card border border-border rounded-xl p-6 w-full max-w-lg max-h-[90vh] overflow-y-auto">
            <div className="flex justify-between items-center mb-6">
              <h2 className="text-xl font-bold text-card-foreground">
                {editingPlan ? 'Editar Plano' : 'Novo Plano'}
              </h2>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setShowModal(false)}
                className="text-muted-foreground hover:text-foreground"
              >
                <X className="w-5 h-5" />
              </Button>
            </div>

            <div className="space-y-4">
              {/* Nome */}
              <div>
                <label className="block text-muted-foreground text-sm mb-1">Nome do Plano</label>
                <Input
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  placeholder="Ex: Pro"
                  className="bg-background border-input text-foreground"
                />
              </div>

              {/* Descrição */}
              <div>
                <label className="block text-muted-foreground text-sm mb-1">Descrição</label>
                <Input
                  value={formData.description}
                  onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                  placeholder="Descrição curta"
                  className="bg-background border-input text-foreground"
                />
              </div>

              {/* Grid 2 colunas */}
              <div className="grid grid-cols-2 gap-4">
                {/* Preço */}
                <div>
                  <label className="block text-muted-foreground text-sm mb-1">
                    Preço Mensal (R$)
                  </label>
                  <Input
                    type="number"
                    value={formData.price_brl}
                    onChange={(e) =>
                      setFormData({ ...formData, price_brl: parseFloat(e.target.value) || 0 })
                    }
                    className="bg-background border-input text-foreground"
                  />
                </div>

                {/* Créditos */}
                <div>
                  <label className="block text-muted-foreground text-sm mb-1">
                    Créditos para Exibição
                  </label>
                  <Input
                    type="number"
                    value={formData.display_credits}
                    onChange={(e) =>
                      setFormData({ ...formData, display_credits: parseInt(e.target.value) || 0 })
                    }
                    className="bg-background border-input text-foreground"
                  />
                </div>
              </div>

              {/* Mensagens estimadas info */}
              <div className="bg-primary border border-primary rounded-lg p-3">
                <p className="text-primary-foreground text-sm">
                  Mensagens estimadas: ~
                  {formatNumber(calcularMensagensEstimadas(formData.price_brl))}*
                </p>
                <p className="text-primary-foreground text-xs mt-1">
                  *estimativa com modelo padrão (gpt-4o-mini)
                </p>
              </div>

              {/* Features Section */}
              <div className="border-t border-border pt-4">
                <label className="block text-muted-foreground text-sm mb-3">
                  Features do Plano (escreva livremente)
                </label>

                {/* Feature List */}
                <div className="space-y-2 mb-4">
                  {formData.features.map((feature, idx) => (
                    <div key={idx} className="flex items-center gap-2">
                      {/* Toggle included */}
                      <button
                        type="button"
                        onClick={() => toggleFeatureIncluded(idx)}
                        className={`w-8 h-8 rounded flex items-center justify-center text-sm ${
                          feature.included
                            ? 'bg-primary/20 text-primary hover:bg-primary/30'
                            : 'bg-danger/10 text-danger hover:bg-danger/30'
                        }`}
                      >
                        {feature.included ? (
                          <Check className="w-4 h-4" />
                        ) : (
                          <X className="w-4 h-4" />
                        )}
                      </button>

                      {/* Feature name input */}
                      <Input
                        value={feature.name}
                        onChange={(e) => updateFeatureName(idx, e.target.value)}
                        className="bg-background border-input text-foreground flex-1"
                      />

                      {/* Delete button */}
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => removeFeature(idx)}
                        className="text-muted-foreground hover:text-destructive"
                      >
                        <Trash2 className="w-4 h-4" />
                      </Button>
                    </div>
                  ))}
                </div>

                {/* Add new feature */}
                <div className="flex gap-2">
                  <Input
                    value={newFeatureName}
                    onChange={(e) => setNewFeatureName(e.target.value)}
                    onKeyPress={(e) => e.key === 'Enter' && addFeature()}
                    placeholder="Nova feature (ex: Integração WhatsApp)"
                    className="bg-background border-input text-foreground flex-1"
                  />
                  <Button
                    type="button"
                    onClick={addFeature}
                    disabled={!newFeatureName.trim()}
                    className="bg-success hover:bg-success/90 text-primary-foreground"
                  >
                    <Plus className="w-4 h-4" />
                  </Button>
                </div>
              </div>

              {/* Ativo e Ordem */}
              <div className="grid grid-cols-2 gap-4 border-t border-border pt-4">
                <div className="flex items-center justify-between">
                  <label className="text-muted-foreground text-sm">Plano Ativo</label>
                  <Switch
                    checked={formData.is_active}
                    onCheckedChange={(checked) => setFormData({ ...formData, is_active: checked })}
                  />
                </div>
                <div>
                  <label className="block text-muted-foreground text-sm mb-1">Ordem</label>
                  <Input
                    type="number"
                    value={formData.display_order}
                    onChange={(e) =>
                      setFormData({ ...formData, display_order: parseInt(e.target.value) || 0 })
                    }
                    className="bg-background border-input text-foreground w-20"
                  />
                </div>
              </div>

              {/* Stripe Integration */}
              <div className="border-t border-border pt-4">
                <label className="block text-muted-foreground text-sm mb-3">
                  Integração Stripe
                </label>
                <div className="space-y-3">
                  <div>
                    <label className="block text-muted-foreground text-xs mb-1">
                      Product ID (opcional)
                    </label>
                    <Input
                      value={formData.stripe_product_id}
                      onChange={(e) =>
                        setFormData({ ...formData, stripe_product_id: e.target.value })
                      }
                      placeholder="prod_xxx"
                      className="bg-background border-input text-foreground font-mono text-sm"
                    />
                  </div>
                  <div>
                    <label className="block text-muted-foreground text-xs mb-1">
                      Price ID (obrigatório para checkout)
                    </label>
                    <Input
                      value={formData.stripe_price_id}
                      onChange={(e) =>
                        setFormData({ ...formData, stripe_price_id: e.target.value })
                      }
                      placeholder="price_xxx"
                      className="bg-background border-input text-foreground font-mono text-sm"
                    />
                  </div>
                  <p className="text-muted-foreground text-xs">
                    Copie o Price ID do{' '}
                    <a
                      href="https://dashboard.stripe.com/products"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-primary hover:underline"
                    >
                      Stripe Dashboard
                    </a>
                  </p>
                </div>
              </div>
            </div>

            {/* Actions */}
            <div className="flex gap-3 mt-6">
              <Button
                variant="outline"
                onClick={() => setShowModal(false)}
                className="flex-1 bg-transparent border-input text-muted-foreground hover:bg-muted"
              >
                Cancelar
              </Button>
              <Button
                onClick={handleSave}
                disabled={saving || !formData.name}
                className="flex-1 bg-primary hover:bg-primary/90 text-primary-foreground"
              >
                {saving ? 'Salvando...' : 'Salvar'}
              </Button>
            </div>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={!!deletePlanId}
        onOpenChange={(isOpen) => {
          if (!isOpen) setDeletePlanId(null);
        }}
        title="Desativar plano?"
        description="Esta ação desativa o plano para novas assinaturas."
        confirmLabel="Desativar"
        destructive
        onConfirm={() => {
          const planId = deletePlanId;
          setDeletePlanId(null);
          if (planId) {
            void handleDelete(planId);
          }
        }}
      />
    </PageShell>
  );
}
