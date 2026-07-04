'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Building2,
  CheckCircle,
  XCircle,
  Pause,
  Plus,
  Edit,
  UserPlus,
  Copy,
  Bot,
} from 'lucide-react';
import { logSystemAction } from '@/lib/logger';
import type { Company } from '@/lib/types';
import { DocumentManagementModal } from '@/components/admin/DocumentManagementModal';
import { EmptyStatePanel, LoadingState } from '@/components/ui/feedback-state';
import {
  PageActions,
  PageDescription,
  PageHeader,
  PageShell,
  PageTitle,
} from '@/components/ui/page-shell';
import { StatusPill } from '@/components/ui/status-pill';

export default function AdminCompaniesPage() {
  const router = useRouter();
  const [companies, setCompanies] = useState<Company[]>([]);
  const [loading, setLoading] = useState(true);
  const [updating, setUpdating] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [editingCompany, setEditingCompany] = useState<Company | null>(null);
  const [inviteDialogOpen, setInviteDialogOpen] = useState(false);
  const [inviteCompany, setInviteCompany] = useState<Company | null>(null);
  const [generatingInvite, setGeneratingInvite] = useState(false);
  const [inviteLink, setInviteLink] = useState('');
  const [adminInviteEmail, setAdminInviteEmail] = useState('');
  const [adminInviteName, setAdminInviteName] = useState('');
  const [adminType, setAdminType] = useState<'owner' | 'regular'>('regular');
  const [fetchingCep, setFetchingCep] = useState(false);
  const [formData, setFormData] = useState({
    company_name: '',
    legal_name: '',
    cnpj: '',
    max_users: '5',
    primary_contact_name: '',
    primary_contact_email: '',
    primary_contact_phone: '',
    notes: '',
    // Address fields
    cep: '',
    street: '',
    number: '',
    complement: '',
    neighborhood: '',
    city: '',
    state: '',
  });

  useEffect(() => {
    loadCompanies();
  }, []);

  const loadCompanies = async () => {
    try {
      const response = await fetch('/api/admin/companies');
      if (!response.ok) throw new Error('Failed to fetch companies');

      const data = await response.json();
      setCompanies(data.companies || []);
    } catch (error) {
      console.error('Error loading companies:', error);
    } finally {
      setLoading(false);
    }
  };

  const updateCompanyStatus = async (companyId: string, newStatus: 'active' | 'suspended') => {
    setUpdating(companyId);
    try {
      const company = companies.find((c) => c.id === companyId);

      const response = await fetch('/api/admin/companies', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: companyId, status: newStatus }),
      });

      if (!response.ok) throw new Error('Failed to update company');

      await logSystemAction({
        companyId,
        actionType: newStatus === 'active' ? 'COMPANY_ACTIVATED' : 'COMPANY_SUSPENDED',
        resourceType: 'company',
        resourceId: companyId,
        details: { companyName: company?.company_name, newStatus },
        status: 'success',
      });

      await loadCompanies();
    } catch (error) {
      console.error('Error updating company:', error);
      await logSystemAction({
        companyId,
        actionType: 'COMPANY_UPDATED',
        resourceType: 'company',
        resourceId: companyId,
        details: { error: String(error), newStatus },
        status: 'error',
        errorMessage: 'Erro ao atualizar empresa',
      });
    } finally {
      setUpdating(null);
    }
  };

  const openEditDialog = (company: Company) => {
    setEditingCompany(company);
    setFormData({
      company_name: company.company_name || '',
      legal_name: company.legal_name || '',
      cnpj: company.cnpj || '',
      max_users: String(company.max_users || 5),
      primary_contact_name: company.primary_contact_name || '',
      primary_contact_email: company.primary_contact_email || '',
      primary_contact_phone: company.primary_contact_phone || '',
      notes: company.notes || '',
      cep: company.cep || '',
      street: company.street || '',
      number: company.number || '',
      complement: company.complement || '',
      neighborhood: company.neighborhood || '',
      city: company.city || '',
      state: company.state || '',
    });
    setDialogOpen(true);
  };

  const openInviteDialog = (company: Company) => {
    setInviteCompany(company);
    setInviteLink('');
    setAdminInviteEmail('');
    setAdminInviteName('');
    setInviteDialogOpen(true);
  };

  const generateAdminInvite = async () => {
    if (!inviteCompany) return;

    if (!adminInviteEmail || !adminInviteEmail.includes('@')) {
      alert('Por favor, informe um email válido');
      return;
    }

    setGeneratingInvite(true);
    try {
      const response = await fetch('/api/invites/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          role: 'admin_company',
          companyId: inviteCompany.id,
          email: adminInviteEmail,
          name: adminInviteName || null,
          isOwner: adminType === 'owner', // NEW: Send owner flag
        }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Falha ao gerar convite');
      }

      setInviteLink(data.inviteLink);

      await logSystemAction({
        companyId: inviteCompany.id,
        actionType: 'INVITE_GENERATED',
        resourceType: 'invite',
        details: {
          role: 'admin_company',
          companyName: inviteCompany.company_name,
          email: adminInviteEmail,
        },
        status: 'success',
      });
    } catch (error: any) {
      console.error('Error generating invite:', error);
      alert(error.message || 'Falha ao gerar convite');
    } finally {
      setGeneratingInvite(false);
    }
  };

  const copyInviteToClipboard = () => {
    navigator.clipboard.writeText(inviteLink);
    alert('Link copiado para área de transferência!');
  };

  const closeDialog = () => {
    setDialogOpen(false);
    setEditingCompany(null);
    setFormData({
      company_name: '',
      legal_name: '',
      cnpj: '',
      max_users: '5',
      primary_contact_name: '',
      primary_contact_email: '',
      primary_contact_phone: '',
      notes: '',
      cep: '',
      street: '',
      number: '',
      complement: '',
      neighborhood: '',
      city: '',
      state: '',
    });
  };

  const createCompany = async () => {
    if (!formData.company_name) {
      alert('Nome da empresa é obrigatório');
      return;
    }

    setCreating(true);
    try {
      const response = await fetch('/api/admin/companies', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          company_name: formData.company_name,
          legal_name: formData.legal_name || null,
          cnpj: formData.cnpj || null,
          webhook_url: '',
          use_langchain: true,
          max_users: parseInt(formData.max_users) || 5,
          primary_contact_name: formData.primary_contact_name || null,
          primary_contact_email: formData.primary_contact_email || null,
          primary_contact_phone: formData.primary_contact_phone || null,
          notes: formData.notes || null,
          cep: formData.cep || null,
          street: formData.street || null,
          number: formData.number || null,
          complement: formData.complement || null,
          neighborhood: formData.neighborhood || null,
          city: formData.city || null,
          state: formData.state || null,
          status: 'active',
        }),
      });

      if (!response.ok) throw new Error('Failed to create company');
      const result = await response.json();

      await logSystemAction({
        companyId: result.company?.id,
        actionType: 'COMPANY_CREATED',
        resourceType: 'company',
        resourceId: result.company?.id,
        details: { companyName: formData.company_name },
        status: 'success',
      });

      closeDialog();
      await loadCompanies();
    } catch (error) {
      console.error('Error creating company:', error);
      await logSystemAction({
        actionType: 'COMPANY_CREATED',
        details: { error: String(error), companyName: formData.company_name },
        status: 'error',
        errorMessage: 'Erro ao criar empresa',
      });
      alert('Erro ao criar empresa');
    } finally {
      setCreating(false);
    }
  };

  const updateCompany = async () => {
    if (!editingCompany) return;
    if (!formData.company_name) {
      alert('Nome da empresa é obrigatório');
      return;
    }

    setCreating(true);
    try {
      const response = await fetch('/api/admin/companies', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: editingCompany.id,
          company_name: formData.company_name,
          legal_name: formData.legal_name || null,
          cnpj: formData.cnpj || null,
          webhook_url: '',
          use_langchain: true,
          max_users: parseInt(formData.max_users) || 5,
          primary_contact_name: formData.primary_contact_name || null,
          primary_contact_email: formData.primary_contact_email || null,
          primary_contact_phone: formData.primary_contact_phone || null,
          notes: formData.notes || null,
          cep: formData.cep || null,
          street: formData.street || null,
          number: formData.number || null,
          complement: formData.complement || null,
          neighborhood: formData.neighborhood || null,
          city: formData.city || null,
          state: formData.state || null,
        }),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || 'Failed to update');
      }

      await logSystemAction({
        companyId: editingCompany.id,
        actionType: 'COMPANY_UPDATED',
        resourceType: 'company',
        resourceId: editingCompany.id,
        details: { companyName: formData.company_name },
        status: 'success',
      });

      closeDialog();
      await loadCompanies();
    } catch (error: any) {
      console.error('Error updating company:', error);
      alert(`Erro ao atualizar empresa: ${error.message || 'Erro desconhecido'}`);

      await logSystemAction({
        companyId: editingCompany.id,
        actionType: 'COMPANY_UPDATED',
        resourceType: 'company',
        resourceId: editingCompany.id,
        details: { error: String(error) },
        status: 'error',
        errorMessage: 'Erro ao atualizar empresa',
      });
    } finally {
      setCreating(false);
    }
  };

  const handleSubmit = () => {
    if (editingCompany) {
      updateCompany();
    } else {
      createCompany();
    }
  };

  const getStatusBadge = (status: string) => {
    switch (status) {
      case 'active':
        return <StatusPill tone="success">Ativa</StatusPill>;
      case 'suspended':
        return <StatusPill tone="danger">Suspensa</StatusPill>;
      case 'trial':
        return <StatusPill tone="brand">Trial</StatusPill>;
      case 'cancelled':
        return <StatusPill>Cancelada</StatusPill>;
      default:
        return <StatusPill>{status}</StatusPill>;
    }
  };

  return (
    <PageShell>
      <PageHeader>
        <div>
          <PageTitle>Gerenciar Empresas</PageTitle>
          <PageDescription>Visualize e gerencie todas as empresas cadastradas</PageDescription>
        </div>
        <PageActions>
          <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
            <DialogTrigger asChild>
              <Button>
                <Plus className="w-4 h-4 mr-2" />
                Nova Empresa
              </Button>
            </DialogTrigger>
            <DialogContent className="bg-card border-border text-foreground max-w-2xl max-h-[90vh] overflow-y-auto">
              <DialogHeader>
                <DialogTitle className="text-foreground text-xl">
                  {editingCompany ? 'Editar Empresa' : 'Cadastrar Nova Empresa'}
                </DialogTitle>
              </DialogHeader>
              <div className="space-y-4 py-4">
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <Label htmlFor="company_name" className="text-muted-foreground">
                      Nome da Empresa *
                    </Label>
                    <Input
                      id="company_name"
                      value={formData.company_name}
                      onChange={(e) => setFormData({ ...formData, company_name: e.target.value })}
                      className="bg-muted border-border text-foreground"
                    />
                  </div>
                  <div>
                    <Label htmlFor="legal_name" className="text-muted-foreground">
                      Razão Social
                    </Label>
                    <Input
                      id="legal_name"
                      value={formData.legal_name}
                      onChange={(e) => setFormData({ ...formData, legal_name: e.target.value })}
                      className="bg-muted border-border text-foreground"
                    />
                  </div>
                </div>
                <div>
                  <Label htmlFor="cnpj" className="text-muted-foreground">
                    CNPJ
                  </Label>
                  <Input
                    id="cnpj"
                    value={formData.cnpj}
                    onChange={(e) => setFormData({ ...formData, cnpj: e.target.value })}
                    className="bg-muted border-border text-foreground"
                    placeholder="00.000.000/0000-00"
                  />
                </div>
                <div>
                  <Label htmlFor="max_users" className="text-muted-foreground">
                    Máximo de Administradores
                    <span className="text-xs text-muted-foreground ml-2">
                      (Usuários de chat são ilimitados)
                    </span>
                  </Label>
                  <Input
                    id="max_users"
                    type="number"
                    min="1"
                    value={formData.max_users}
                    onChange={(e) => setFormData({ ...formData, max_users: e.target.value })}
                    className="bg-muted border-border text-foreground w-32"
                  />
                </div>

                {/* Seção Endereço com CEP */}
                <div className="border-t border-border pt-4">
                  <h3 className="text-foreground font-medium mb-3">Endereço</h3>
                  <div className="space-y-3">
                    <div className="grid grid-cols-4 gap-4">
                      <div className="col-span-1">
                        <Label htmlFor="cep" className="text-muted-foreground">
                          CEP
                        </Label>
                        <div className="relative">
                          <Input
                            id="cep"
                            value={formData.cep}
                            onChange={(e) => setFormData({ ...formData, cep: e.target.value })}
                            onBlur={async (e) => {
                              const cep = e.target.value.replace(/\D/g, '');
                              if (cep.length !== 8) return;
                              setFetchingCep(true);
                              try {
                                const res = await fetch(`https://viacep.com.br/ws/${cep}/json/`);
                                const data = await res.json();
                                if (!data.erro) {
                                  setFormData((prev) => ({
                                    ...prev,
                                    street: data.logradouro || '',
                                    neighborhood: data.bairro || '',
                                    city: data.localidade || '',
                                    state: data.uf || '',
                                  }));
                                }
                              } catch (err) {
                                console.error('Erro ao buscar CEP:', err);
                              } finally {
                                setFetchingCep(false);
                              }
                            }}
                            className="bg-muted border-border text-foreground"
                            placeholder="00000-000"
                          />
                          {fetchingCep && (
                            <span className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-muted-foreground">
                              Buscando...
                            </span>
                          )}
                        </div>
                      </div>
                      <div className="col-span-3">
                        <Label htmlFor="street" className="text-muted-foreground">
                          Logradouro
                        </Label>
                        <Input
                          id="street"
                          value={formData.street}
                          onChange={(e) => setFormData({ ...formData, street: e.target.value })}
                          className="bg-muted border-border text-foreground"
                        />
                      </div>
                    </div>
                    <div className="grid grid-cols-4 gap-4">
                      <div className="col-span-1">
                        <Label htmlFor="number" className="text-muted-foreground">
                          Número
                        </Label>
                        <Input
                          id="number"
                          value={formData.number}
                          onChange={(e) => setFormData({ ...formData, number: e.target.value })}
                          className="bg-muted border-border text-foreground"
                        />
                      </div>
                      <div className="col-span-1">
                        <Label htmlFor="complement" className="text-muted-foreground">
                          Complemento
                        </Label>
                        <Input
                          id="complement"
                          value={formData.complement}
                          onChange={(e) => setFormData({ ...formData, complement: e.target.value })}
                          className="bg-muted border-border text-foreground"
                          placeholder="Apto, Sala..."
                        />
                      </div>
                      <div className="col-span-2">
                        <Label htmlFor="neighborhood" className="text-muted-foreground">
                          Bairro
                        </Label>
                        <Input
                          id="neighborhood"
                          value={formData.neighborhood}
                          onChange={(e) =>
                            setFormData({ ...formData, neighborhood: e.target.value })
                          }
                          className="bg-muted border-border text-foreground"
                        />
                      </div>
                    </div>
                    <div className="grid grid-cols-4 gap-4">
                      <div className="col-span-3">
                        <Label htmlFor="city" className="text-muted-foreground">
                          Cidade
                        </Label>
                        <Input
                          id="city"
                          value={formData.city}
                          onChange={(e) => setFormData({ ...formData, city: e.target.value })}
                          className="bg-muted border-border text-foreground"
                        />
                      </div>
                      <div className="col-span-1">
                        <Label htmlFor="state" className="text-muted-foreground">
                          UF
                        </Label>
                        <Input
                          id="state"
                          value={formData.state}
                          maxLength={2}
                          onChange={(e) =>
                            setFormData({ ...formData, state: e.target.value.toUpperCase() })
                          }
                          className="bg-muted border-border text-foreground"
                          placeholder="SP"
                        />
                      </div>
                    </div>
                  </div>
                </div>
                <div className="border-t border-border pt-4">
                  <h3 className="text-foreground font-medium mb-3">Contato Principal</h3>
                  <div className="space-y-3">
                    <div>
                      <Label htmlFor="primary_contact_name" className="text-muted-foreground">
                        Nome
                      </Label>
                      <Input
                        id="primary_contact_name"
                        value={formData.primary_contact_name}
                        onChange={(e) =>
                          setFormData({ ...formData, primary_contact_name: e.target.value })
                        }
                        className="bg-muted border-border text-foreground"
                      />
                    </div>
                    <div className="grid grid-cols-2 gap-4">
                      <div>
                        <Label htmlFor="primary_contact_email" className="text-muted-foreground">
                          Email
                        </Label>
                        <Input
                          id="primary_contact_email"
                          type="email"
                          value={formData.primary_contact_email}
                          onChange={(e) =>
                            setFormData({ ...formData, primary_contact_email: e.target.value })
                          }
                          className="bg-muted border-border text-foreground"
                        />
                      </div>
                      <div>
                        <Label htmlFor="primary_contact_phone" className="text-muted-foreground">
                          Telefone
                        </Label>
                        <Input
                          id="primary_contact_phone"
                          value={formData.primary_contact_phone}
                          onChange={(e) =>
                            setFormData({ ...formData, primary_contact_phone: e.target.value })
                          }
                          className="bg-muted border-border text-foreground"
                        />
                      </div>
                    </div>
                  </div>
                </div>
                <div>
                  <Label htmlFor="notes" className="text-muted-foreground">
                    Observações Internas
                  </Label>
                  <Textarea
                    id="notes"
                    value={formData.notes}
                    onChange={(e) => setFormData({ ...formData, notes: e.target.value })}
                    className="bg-muted border-border text-foreground"
                    rows={3}
                  />
                </div>
                <div className="flex gap-2 pt-4">
                  <Button onClick={handleSubmit} disabled={creating} className="flex-1">
                    {creating
                      ? 'Salvando...'
                      : editingCompany
                        ? 'Atualizar Empresa'
                        : 'Salvar Empresa'}
                  </Button>
                  <Button
                    onClick={closeDialog}
                    variant="outline"
                    className="flex-1 bg-transparent border-border text-muted-foreground hover:text-foreground hover:bg-muted"
                  >
                    Cancelar
                  </Button>
                </div>
              </div>
            </DialogContent>
          </Dialog>
        </PageActions>
      </PageHeader>

      {loading ? (
        <LoadingState label="Carregando empresas..." />
      ) : (
        <div className="space-y-4">
          {companies.map((company) => (
            <Card key={company.id} className="bg-card border-border">
              <CardHeader>
                <div className="flex items-start justify-between">
                  <div className="flex items-center gap-4">
                    <div className="w-12 h-12 bg-primary rounded-lg flex items-center justify-center">
                      <Building2 className="w-6 h-6 text-primary-foreground" />
                    </div>
                    <div>
                      <div className="flex items-center gap-2 flex-wrap">
                        <CardTitle className="text-foreground text-xl">
                          {company.company_name}
                        </CardTitle>
                        {(company as any).subscription && (
                          <>
                            <Badge className="bg-primary text-primary-foreground border-transparent hover:bg-primary/90">
                              {(company as any).subscription.plan_name} • R${' '}
                              {(company as any).subscription.plan_price.toLocaleString('pt-BR', {
                                minimumFractionDigits: 2,
                                maximumFractionDigits: 2,
                              })}
                              /mês
                            </Badge>
                            {(company as any).subscription.current_period_end && (
                              <Badge className="bg-primary text-primary-foreground border-transparent hover:bg-primary/90">
                                Vence:{' '}
                                {new Date(
                                  (company as any).subscription.current_period_end,
                                ).toLocaleDateString('pt-BR')}
                              </Badge>
                            )}
                            <Badge className="bg-primary text-primary-foreground border-transparent hover:bg-primary/90">
                              {((company as any).credits_remaining || 0).toLocaleString('pt-BR')} /{' '}
                              {((company as any).subscription.display_credits || 0).toLocaleString(
                                'pt-BR',
                              )}{' '}
                              créditos
                            </Badge>
                            {(company as any).subscription.status === 'past_due' && (
                              <Badge className="bg-danger/10 text-danger border-danger/20">
                                Pagamento pendente
                              </Badge>
                            )}
                          </>
                        )}
                      </div>
                      <p className="text-sm text-muted-foreground mt-1">
                        CNPJ: {company.cnpj || 'Não informado'}
                      </p>
                    </div>
                  </div>
                  {getStatusBadge(company.status)}
                </div>
              </CardHeader>
              <CardContent>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">
                  <div>
                    <p className="text-xs text-muted-foreground mb-1">Máx. Administradores</p>
                    <p className="text-foreground font-medium">{company.max_users || 5}</p>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground mb-1">Criado em</p>
                    <p className="text-foreground font-medium">
                      {new Date(company.created_at).toLocaleDateString('pt-BR')}
                    </p>
                  </div>
                </div>

                <div className="flex gap-2">
                  <Button
                    onClick={() => openEditDialog(company)}
                    variant="outline"
                    className="bg-transparent border-foreground text-foreground hover:text-foreground hover:bg-muted"
                  >
                    <Edit className="w-4 h-4 mr-2" />
                    Editar
                  </Button>
                  <DocumentManagementModal
                    companyId={company.id}
                    companyName={company.company_name}
                  />
                  <Button
                    onClick={() => router.push(`/admin/companies/${company.id}/agents`)}
                    variant="outline"
                    className="bg-transparent border-foreground text-foreground hover:text-foreground hover:bg-muted"
                  >
                    <Bot className="w-4 h-4 mr-2" />
                    Agentes
                  </Button>
                  <Button
                    onClick={() => openInviteDialog(company)}
                    variant="outline"
                    className="bg-transparent border-foreground text-foreground hover:text-foreground hover:bg-muted"
                  >
                    <UserPlus className="w-4 h-4 mr-2" />
                    Convidar Admin
                  </Button>
                  {company.status === 'suspended' ? (
                    <Button
                      onClick={() => updateCompanyStatus(company.id, 'active')}
                      disabled={updating === company.id}
                      className="bg-success hover:bg-success/90 text-primary-foreground"
                    >
                      <CheckCircle className="w-4 h-4 mr-2" />
                      {updating === company.id ? 'Ativando...' : 'Ativar Empresa'}
                    </Button>
                  ) : (
                    <Button
                      onClick={() => updateCompanyStatus(company.id, 'suspended')}
                      disabled={updating === company.id}
                      variant="destructive"
                    >
                      <Pause className="w-4 h-4 mr-2" />
                      {updating === company.id ? 'Suspendendo...' : 'Suspender Empresa'}
                    </Button>
                  )}
                </div>
              </CardContent>
            </Card>
          ))}

          {companies.length === 0 && (
            <EmptyStatePanel
              icon={Building2}
              title="Nenhuma empresa cadastrada"
              description="As empresas aparecerão aqui quando forem cadastradas no sistema."
            />
          )}
        </div>
      )}

      {/* Invite Dialog */}
      <Dialog open={inviteDialogOpen} onOpenChange={setInviteDialogOpen}>
        <DialogContent className="bg-card border-border text-foreground">
          <DialogHeader>
            <DialogTitle>Gerar Convite de Administrador</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <p className="text-sm text-muted-foreground">
              Empresa:{' '}
              <span className="text-foreground font-medium">{inviteCompany?.company_name}</span>
            </p>

            {!inviteLink ? (
              <>
                <p className="text-sm text-muted-foreground">
                  Informe o email do primeiro administrador desta empresa.
                </p>

                {/* Email Input */}
                <div className="space-y-2">
                  <Label className="text-foreground">Email do Administrador *</Label>
                  <Input
                    type="email"
                    placeholder="joao@empresa.com"
                    value={adminInviteEmail}
                    onChange={(e) => setAdminInviteEmail(e.target.value)}
                    className="bg-muted border-border text-foreground"
                  />
                </div>

                {/* Name Input */}
                <div className="space-y-2">
                  <Label className="text-foreground">Nome (Opcional)</Label>
                  <Input
                    type="text"
                    placeholder="João Silva"
                    value={adminInviteName}
                    onChange={(e) => setAdminInviteName(e.target.value)}
                    className="bg-muted border-border text-foreground"
                  />
                </div>

                {/* Admin Type Selection */}
                <div className="space-y-3">
                  <Label className="text-foreground">Tipo de Administrador</Label>
                  <div className="space-y-2">
                    <label className="flex items-center gap-3 p-3 bg-muted rounded-lg border border-border cursor-pointer hover:border-accent/30 transition-colors">
                      <input
                        type="radio"
                        name="adminType"
                        value="regular"
                        checked={adminType === 'regular'}
                        onChange={() => setAdminType('regular')}
                        className="w-4 h-4 text-accent"
                      />
                      <div className="flex-1">
                        <p className="text-foreground font-medium">Administrador Regular</p>
                        <p className="text-xs text-muted-foreground">
                          Pode criar membros | Requer aprovação
                        </p>
                      </div>
                    </label>

                    <label className="flex items-center gap-3 p-3 bg-muted rounded-lg border border-border cursor-pointer hover:border-warning/50 transition-colors">
                      <input
                        type="radio"
                        name="adminType"
                        value="owner"
                        checked={adminType === 'owner'}
                        onChange={() => setAdminType('owner')}
                        className="w-4 h-4 text-warning"
                      />
                      <div className="flex-1">
                        <p className="text-foreground font-medium">Proprietário (Owner)</p>
                        <p className="text-xs text-muted-foreground">
                          Pode criar outros admins + membros | Requer aprovação
                        </p>
                      </div>
                    </label>
                  </div>
                </div>

                <Button
                  onClick={generateAdminInvite}
                  disabled={generatingInvite || !adminInviteEmail}
                  className="w-full disabled:opacity-50"
                >
                  {generatingInvite ? 'Gerando...' : 'Gerar Convite Nominal'}
                </Button>
              </>
            ) : (
              <>
                <p className="text-sm text-success">
                  Convite gerado para:{' '}
                  <strong>
                    {inviteLink.split('email=')[1]?.split('&')[0] || adminInviteEmail}
                  </strong>
                </p>
                <div className="flex gap-2">
                  <Input
                    value={inviteLink}
                    readOnly
                    className="bg-muted border-border text-foreground text-sm"
                  />
                  <Button
                    onClick={copyInviteToClipboard}
                    className="bg-primary hover:bg-primary/90"
                  >
                    <Copy className="w-4 h-4" />
                  </Button>
                </div>
                <p className="text-xs text-muted-foreground">
                  Este link é exclusivo para {adminInviteEmail} e expira em 7 dias.
                </p>
              </>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </PageShell>
  );
}
