// app/admin/team/page.tsx

'use client';

import { useState, useEffect } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { EmptyStatePanel, LoadingState } from '@/components/ui/feedback-state';
import { PageDescription, PageHeader, PageShell, PageTitle } from '@/components/ui/page-shell';
import { StatusPill } from '@/components/ui/status-pill';
import {
  Users,
  UserPlus,
  CheckCircle,
  Copy,
  Clock,
  Shield,
  Crown,
  XCircle,
  PlayCircle,
} from 'lucide-react';
import { toast } from 'sonner';
import { useAdminRole } from '@/hooks/useAdminRole';
import { useRouter } from 'next/navigation';

interface User {
  id: string;
  email: string;
  first_name: string;
  last_name: string;
  role: string;
  status: string;
  is_owner?: boolean;
  created_at: string;
}

export default function TeamManagementPage() {
  const { role, companyId, userId: currentUserId, isOwner, isLoading: roleLoading } = useAdminRole();
  const router = useRouter();

  const [users, setUsers] = useState<User[]>([]);
  const [pendingUsers, setPendingUsers] = useState<User[]>([]);
  const [loading, setLoading] = useState(true);

  // Estados do Convite
  const [generating, setGenerating] = useState(false);
  const [inviteLink, setInviteLink] = useState('');
  const [showInviteDialog, setShowInviteDialog] = useState(false);
  const [selectedRole, setSelectedRole] = useState<'member' | 'admin_company'>('member');
  const [inviteEmail, setInviteEmail] = useState('');
  const [inviteName, setInviteName] = useState('');

  useEffect(() => {
    if (!roleLoading) {
      if (role !== 'company_admin' && role !== 'master') {
        router.push('/admin');
        return;
      }
      loadTeam();
    }
  }, [role, roleLoading, companyId, isOwner]);

  const loadTeam = async () => {
    if (!companyId) return;
    setLoading(true);

    try {
      const response = await fetch('/api/admin/team');
      if (!response.ok) throw new Error('Failed to load team');

      const data = await response.json();
      setUsers(data.users || []);
      setPendingUsers(data.pendingUsers || []);
    } catch (error) {
      console.error('Error loading team:', error);
      toast.error('Falha ao carregar equipe');
    } finally {
      setLoading(false);
    }
  };

  const generateInvite = async () => {
    if (!inviteEmail || !inviteEmail.includes('@')) {
      toast.error('Por favor, informe um email válido');
      return;
    }

    setGenerating(true);
    try {
      const response = await fetch('/api/invites/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          role: selectedRole,
          companyId: companyId,
          email: inviteEmail,
          name: inviteName || null,
          isOwner: false, // Company Admin NUNCA cria outro Owner por aqui
        }),
      });

      let data: any = {};
      const responseText = await response.text();

      try {
        if (responseText) {
          data = JSON.parse(responseText);
        }
      } catch (e) {
        console.warn('[Invite] Failed to parse response:', responseText);
        data = { error: response.statusText || 'Erro desconhecido' };
      }

      if (!response.ok) {
        console.warn('[Invite] Generation failed. Status:', response.status, 'Body:', data);
        toast.error(data.error || 'Falha ao gerar convite. O usuário já pode estar cadastrado.', {
          duration: 5000,
        });
        return;
      }

      setInviteLink(data.inviteLink);
      setShowInviteDialog(true);

      setInviteEmail('');
      setInviteName('');

      toast.success(`Convite gerado! Enviado para ${inviteEmail}`);
    } catch (error: any) {
      console.warn('[Invite] Unexpected error:', error);
      toast.error(error.message || 'Ocorreu um erro ao tentar gerar o convite.');
    } finally {
      setGenerating(false);
    }
  };

  const copyToClipboard = () => {
    navigator.clipboard.writeText(inviteLink);
    toast.success('Link copiado para área de transferência');
  };

  const approveUser = async (userId: string) => {
    try {
      const response = await fetch('/api/admin/team/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ userId }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Falha ao aprovar usuário');
      }

      toast.success(data.message || 'Usuário aprovado!');

      await loadTeam();
    } catch (error: any) {
      console.error('Error approving user:', error);
      toast.error(error.message || 'Falha ao aprovar usuário');
    }
  };

  // Nova função para atualizar status (Suspender/Ativar)
  const updateUserStatus = async (userId: string, newStatus: 'active' | 'suspended') => {
    try {
      const response = await fetch('/api/admin/users/status', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ userId, status: newStatus }),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || 'Failed to update status');
      }

      if (newStatus === 'suspended') {
        toast.warning(`Usuário suspenso com sucesso.`);
      } else {
        toast.success(`Usuário reativado com sucesso.`);
      }

      await loadTeam();
    } catch (error: any) {
      console.error('Error updating user status:', error);
      toast.error('Falha ao atualizar status do usuário');
    }
  };

  const getRoleBadge = (user: User) => {
    if (user.is_owner) {
      return (
        <StatusPill tone="warning" className="gap-1">
          <Crown className="w-3 h-3" /> Dono
        </StatusPill>
      );
    }

    if (user.role === 'admin_company' || user.role === 'owner' || user.role === 'admin') {
      return (
        <StatusPill tone="brand" className="gap-1">
          <Shield className="w-3 h-3" /> Admin
        </StatusPill>
      );
    }

    return <StatusPill tone="info">Membro</StatusPill>;
  };

  // Lógica para determinar se o usuário atual pode gerenciar o alvo
  const canManageUser = (targetUser: User) => {
    // Não pode gerenciar a si mesmo
    if (targetUser.id === currentUserId) return false;

    // Master pode tudo
    if (role === 'master') return true;

    // Owner pode gerenciar qualquer um que NÃO seja owner
    if (isOwner) {
      return !targetUser.is_owner;
    }

    // Admin Company (que não é owner) pode gerenciar apenas membros
    if (role === 'company_admin') {
      return targetUser.role === 'member';
    }

    return false;
  };

  const canInviteAdmin = role === 'master' || (role === 'company_admin' && isOwner);

  if (roleLoading || loading) {
    return <LoadingState label="Carregando equipe..." className="h-full" />;
  }

  return (
    <PageShell size="default">
      <PageHeader>
        <div>
          <PageTitle>Gerenciar Equipe</PageTitle>
          <PageDescription>Convide e gerencie os membros da sua empresa</PageDescription>
        </div>
      </PageHeader>

      {/* CARD DE CONVITE */}
      <Card className="bg-card border-border mb-8">
        <CardHeader>
          <CardTitle className="text-card-foreground flex items-center gap-2">
            <UserPlus className="w-5 h-5 text-primary" />
            Convidar Novo Colaborador
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div className="space-y-4">
              <div className="space-y-2">
                <label className="text-sm font-medium text-foreground ">Email *</label>
                <Input
                  type="email"
                  placeholder="email@empresa.com"
                  value={inviteEmail}
                  onChange={(e) => setInviteEmail(e.target.value)}
                  className="bg-background border-input text-foreground"
                />
              </div>
              <div className="space-y-2">
                <label className="text-sm font-medium text-foreground ">Nome (Opcional)</label>
                <Input
                  type="text"
                  placeholder="Nome do colaborador"
                  value={inviteName}
                  onChange={(e) => setInviteName(e.target.value)}
                  className="bg-background border-input text-foreground"
                />
              </div>
            </div>

            <div className="space-y-4">
              <label className="text-sm font-medium text-foreground ">Nível de Acesso:</label>
              <div className="space-y-3">
                <label
                  className={`flex items-center gap-3 p-3 rounded-lg border cursor-pointer transition-colors ${
                    selectedRole === 'member'
                      ? 'bg-brand-muted border-primary/30'
                      : 'bg-background border-border hover:bg-muted'
                  }`}
                >
                  <input
                    type="radio"
                    name="role"
                    value="member"
                    checked={selectedRole === 'member'}
                    onChange={() => setSelectedRole('member')}
                    className="w-4 h-4 text-primary"
                  />
                  <div>
                    <p className="text-foreground font-medium">Membro</p>
                    <p className="text-xs text-muted-foreground">Acesso apenas ao chat</p>
                  </div>
                </label>

                {canInviteAdmin && (
                  <label
                    className={`flex items-center gap-3 p-3 rounded-lg border cursor-pointer transition-colors ${
                      selectedRole === 'admin_company'
                        ? 'bg-brand-muted border-primary/30'
                        : 'bg-background border-border hover:bg-muted'
                    }`}
                  >
                    <input
                      type="radio"
                      name="role"
                      value="admin_company"
                      checked={selectedRole === 'admin_company'}
                      onChange={() => setSelectedRole('admin_company')}
                      className="w-4 h-4 text-primary"
                    />
                    <div>
                      <p className="text-foreground font-medium">Administrador</p>
                      <p className="text-xs text-muted-foreground">
                        Acesso total às configurações (menos billing)
                      </p>
                    </div>
                  </label>
                )}
              </div>
            </div>
          </div>

          <Button
            onClick={generateInvite}
            disabled={generating}
            className="w-full bg-primary hover:bg-primary/90 text-primary-foreground"
          >
            {generating ? 'Gerando e Enviando...' : 'Gerar Convite'}
          </Button>
        </CardContent>
      </Card>

      <Dialog open={showInviteDialog} onOpenChange={setShowInviteDialog}>
        <DialogContent className="bg-card border-border text-foreground">
          <DialogHeader>
            <DialogTitle>Convite Gerado</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <p className="text-sm text-success">
              E-mail enviado para <strong>{inviteEmail}</strong>
            </p>
            <p className="text-xs text-muted-foreground">
              Caso o email falhe, você pode copiar o link abaixo manualmente:
            </p>
            <div className="flex gap-2">
              <Input
                value={inviteLink}
                readOnly
                className="bg-muted border-input text-foreground text-sm"
              />
              <Button
                onClick={copyToClipboard}
                className="bg-primary hover:bg-primary/90 text-primary-foreground"
              >
                <Copy className="w-4 h-4" />
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      {pendingUsers.length > 0 && (
        <Card className="bg-card border-border mb-8 border-l-4 border-l-warning">
          <CardHeader>
            <CardTitle className="text-card-foreground flex items-center gap-2">
              <Clock className="w-5 h-5 text-warning" />
              Aprovações Pendentes ({pendingUsers.length})
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-3">
              {pendingUsers.map((user) => (
                <div
                  key={user.id}
                  className="flex items-center justify-between p-4 bg-background rounded-lg border border-border"
                >
                  <div>
                    <p className="text-foreground font-medium">
                      {user.first_name} {user.last_name}
                    </p>
                    <p className="text-sm text-muted-foreground">{user.email}</p>
                    <div className="flex gap-2 mt-1">
                      {getRoleBadge(user)}
                      <span className="text-xs text-muted-foreground flex items-center">
                        • Solicitado em {new Date(user.created_at).toLocaleDateString()}
                      </span>
                    </div>
                  </div>
                  <Button
                    onClick={() => approveUser(user.id)}
                    className="bg-success hover:bg-success/90 text-primary-foreground size-sm"
                  >
                    <CheckCircle className="w-4 h-4 mr-2" /> Aprovar
                  </Button>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {/* LISTA DE EQUIPE (ATIVOS E SUSPENSOS) */}
      <Card className="bg-card border-border">
        <CardHeader>
          <CardTitle className="text-card-foreground flex items-center gap-2">
            <Users className="w-5 h-5" />
            Equipe ({users.length})
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="space-y-3">
            {users.map((user) => (
              <div
                key={user.id}
                className="flex items-center justify-between p-4 bg-background rounded-lg border border-border"
              >
                <div className="flex items-center gap-4">
                  <div
                    className={`w-10 h-10 rounded-full flex items-center justify-center text-primary-foreground font-bold ${
                      user.status === 'suspended' ? 'bg-danger' : 'bg-primary'
                    }`}
                  >
                    {user.first_name?.[0]}
                    {user.last_name?.[0]}
                  </div>
                  <div>
                    <div className="flex items-center gap-2">
                      <p
                        className={`font-medium ${user.status === 'suspended' ? 'text-danger line-through' : 'text-foreground'}`}
                      >
                        {user.first_name} {user.last_name}
                      </p>
                      {getRoleBadge(user)}
                    </div>
                    <p className="text-sm text-muted-foreground">{user.email}</p>
                  </div>
                </div>

                <div className="flex items-center gap-4">
                  <div className="text-right">
                    <p className="text-xs text-muted-foreground">
                      Desde {new Date(user.created_at).toLocaleDateString()}
                    </p>
                    <StatusPill tone={user.status === 'suspended' ? 'danger' : 'success'}>
                      {user.status === 'suspended' ? 'Suspenso' : 'Ativo'}
                    </StatusPill>
                  </div>

                  {/* Botões de Ação */}
                  {canManageUser(user) && (
                    <div className="flex gap-2">
                      {user.status === 'suspended' ? (
                        <Button
                          size="icon"
                          variant="ghost"
                          onClick={() => updateUserStatus(user.id, 'active')}
                          className="hover:bg-success/10 text-success"
                          title="Reativar Usuário"
                        >
                          <PlayCircle className="w-5 h-5" />
                        </Button>
                      ) : (
                        <Button
                          size="icon"
                          variant="ghost"
                          onClick={() => updateUserStatus(user.id, 'suspended')}
                          className="hover:bg-danger/10 text-danger"
                          title="Suspender Usuário"
                        >
                          <XCircle className="w-5 h-5" />
                        </Button>
                      )}
                    </div>
                  )}
                </div>
              </div>
            ))}

            {users.length === 0 && (
              <EmptyStatePanel
                icon={Users}
                title="Nenhum membro encontrado"
                description="Os membros aprovados aparecerão aqui."
              />
            )}
          </div>
        </CardContent>
      </Card>
    </PageShell>
  );
}
