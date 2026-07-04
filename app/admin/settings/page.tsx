'use client';

import { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { useAdminRole } from '@/hooks/useAdminRole';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { toast } from 'sonner';
import { Loader2, Lock, User, Save, Headset } from 'lucide-react';
import { AvatarUpload } from '@/components/AvatarUpload';
import { PageDescription, PageHeader, PageShell, PageTitle } from '@/components/ui/page-shell';
import { AttendanceSlaSettings } from '@/components/admin/AttendanceSlaSettings';
import { CompanyAttendanceSettings } from '@/components/admin/CompanyAttendanceSettings';

export default function AdminSettingsPage() {
  const router = useRouter();
  const { role, isLoading: roleLoading } = useAdminRole();
  const [userId, setUserId] = useState<string | null>(null);
  const [sessionType, setSessionType] = useState<'master_admin' | 'company_admin' | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);

  // Profile State
  const [profile, setProfile] = useState({
    first_name: '',
    last_name: '',
    email: '',
    cpf: '',
    birth_date: '',
    avatar_url: '',
  });

  // Password State
  const [passwords, setPasswords] = useState({
    current: '',
    new: '',
    confirm: '',
  });

  // Fetch admin profile from secure endpoint
  useEffect(() => {
    const fetchAdminProfile = async () => {
      try {
        const response = await fetch('/api/admin/me', { credentials: 'include' });

        if (response.status === 403) {
          // Member trying to access admin - redirect
          toast.error('Acesso negado. Você não tem permissão de administrador.');
          router.push('/dashboard');
          return;
        }

        if (response.status === 401) {
          // No session - redirect to login
          router.push('/admin/login');
          return;
        }

        if (response.ok) {
          const data = await response.json();
          if (data.user) {
            setUserId(data.user.id);
            setSessionType(data.sessionType || 'company_admin');
            setProfile({
              first_name: data.user.first_name || '',
              last_name: data.user.last_name || '',
              email: data.user.email || '',
              cpf: data.user.cpf || '',
              birth_date: data.user.birth_date || '',
              avatar_url: data.user.avatar_url || '',
            });
            setIsLoading(false);
          }
        }
      } catch (error) {
        console.error('Error fetching admin profile:', error);
        toast.error('Erro ao carregar perfil.');
        setIsLoading(false);
      }
    };

    if (!roleLoading) {
      fetchAdminProfile();
    }
  }, [roleLoading, router]);

  const handleUpdateProfile = async () => {
    setIsSaving(true);
    try {
      const response = await fetch('/api/admin/profile', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          userId,
          first_name: profile.first_name,
          last_name: profile.last_name,
          avatar_url: profile.avatar_url,
        }),
      });

      if (!response.ok) throw new Error('Failed to update profile');
      toast.success('Perfil atualizado com sucesso!');
    } catch (error) {
      console.error('Erro ao atualizar:', error);
      toast.error('Falha ao atualizar perfil.');
    } finally {
      setIsSaving(false);
    }
  };

  const handleChangePassword = async () => {
    if (!passwords.current || !passwords.new || !passwords.confirm) {
      toast.warning('Preencha todos os campos de senha.');
      return;
    }

    if (passwords.new !== passwords.confirm) {
      toast.error('A nova senha e a confirmação não conferem.');
      return;
    }

    if (passwords.new.length < 6) {
      toast.error('A nova senha deve ter pelo menos 6 caracteres.');
      return;
    }

    setIsSaving(true);
    try {
      // Master Admin uses dedicated API (admin_users table)
      // Company Admin uses standard API (users_v2 table)
      const apiEndpoint =
        sessionType === 'master_admin' ? '/api/admin/change-password' : '/api/auth/change-password';

      const requestBody =
        sessionType === 'master_admin'
          ? { currentPassword: passwords.current, newPassword: passwords.new }
          : { userId: userId, currentPassword: passwords.current, newPassword: passwords.new };

      const response = await fetch(apiEndpoint, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        credentials: 'include',
        body: JSON.stringify(requestBody),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Erro ao alterar senha');
      }

      toast.success('Senha alterada com sucesso!');
      setPasswords({ current: '', new: '', confirm: '' });
    } catch (error: any) {
      console.error('Erro ao mudar senha:', error);
      toast.error(error.message || 'Erro ao alterar senha.');
    } finally {
      setIsSaving(false);
    }
  };

  if (isLoading || roleLoading) {
    return (
      <div className="flex h-screen items-center justify-center text-muted-foreground">
        <Loader2 className="h-8 w-8 animate-spin mr-2" /> Carregando...
      </div>
    );
  }

  return (
    <PageShell size="default">
      <div className="space-y-8">
        <PageHeader>
          <div>
            <PageTitle>Configurações</PageTitle>
            <PageDescription>Gerencie seu perfil e segurança.</PageDescription>
          </div>
        </PageHeader>

        <Tabs defaultValue="profile" className="w-full">
          <TabsList className="grid w-full grid-cols-3 bg-muted border border-border">
            <TabsTrigger
              value="profile"
              className="data-[state=active]:bg-background data-[state=active]:text-foreground text-muted-foreground"
            >
              <User className="h-4 w-4 mr-2" /> Perfil
            </TabsTrigger>
            <TabsTrigger
              value="security"
              className="data-[state=active]:bg-background data-[state=active]:text-foreground text-muted-foreground"
            >
              <Lock className="h-4 w-4 mr-2" /> Segurança
            </TabsTrigger>
            <TabsTrigger
              value="attendance"
              className="data-[state=active]:bg-background data-[state=active]:text-foreground text-muted-foreground"
            >
              <Headset className="h-4 w-4 mr-2" /> Atendimento e SLA
            </TabsTrigger>
          </TabsList>

          <TabsContent value="profile" className="mt-6">
            <Card className="bg-card border-border">
              <CardHeader>
                <CardTitle className="text-foreground">Seu Perfil</CardTitle>
                <CardDescription>Informações do administrador.</CardDescription>
              </CardHeader>
              <CardContent className="space-y-6">
                {/* Avatar Upload */}
                <div className="flex items-center gap-6">
                  {userId && (
                    <AvatarUpload
                      currentImageUrl={profile.avatar_url}
                      onUpload={(url) => setProfile({ ...profile, avatar_url: url })}
                      uploadPath="admins"
                      entityId={userId}
                      size="h-24 w-24"
                      fallback={`${profile.first_name} ${profile.last_name}`}
                    />
                  )}
                  <div>
                    <h3 className="text-lg font-medium text-foreground">Foto de Perfil</h3>
                    <p className="text-sm text-muted-foreground">Clique na imagem para alterar</p>
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-2">
                    <Label className="text-foreground">Nome</Label>
                    <Input
                      value={profile.first_name}
                      onChange={(e) => setProfile({ ...profile, first_name: e.target.value })}
                      className="bg-background border-input text-foreground"
                    />
                  </div>
                  <div className="space-y-2">
                    <Label className="text-foreground">Sobrenome</Label>
                    <Input
                      value={profile.last_name}
                      onChange={(e) => setProfile({ ...profile, last_name: e.target.value })}
                      className="bg-background border-input text-foreground"
                    />
                  </div>
                </div>

                <div className="grid grid-cols-3 gap-4 pt-4">
                  <div className="space-y-2">
                    <Label className="text-foreground">Email</Label>
                    <Input
                      value={profile.email}
                      disabled
                      className="bg-muted border-input text-foreground  opacity-100"
                    />
                  </div>
                  <div className="space-y-2">
                    <Label className="text-foreground">CPF</Label>
                    <Input
                      value={profile.cpf}
                      disabled
                      className="bg-muted border-input text-foreground  opacity-100"
                    />
                  </div>
                  <div className="space-y-2">
                    <Label className="text-foreground">Data de Nascimento</Label>
                    <Input
                      type="date"
                      value={profile.birth_date}
                      disabled
                      className="bg-muted border-input text-foreground  opacity-100"
                    />
                  </div>
                </div>

                <div className="pt-4 flex justify-end">
                  <Button
                    onClick={handleUpdateProfile}
                    disabled={isSaving}
                    className="bg-primary hover:bg-primary/90 text-primary-foreground"
                  >
                    {isSaving ? (
                      <Loader2 className="h-4 w-4 animate-spin mr-2" />
                    ) : (
                      <Save className="h-4 w-4 mr-2" />
                    )}{' '}
                    Salvar
                  </Button>
                </div>
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="security" className="mt-6">
            <Card className="bg-card border-border">
              <CardHeader>
                <CardTitle className="text-foreground">Alterar Senha</CardTitle>
                <CardDescription>Para sua segurança, confirme a senha atual.</CardDescription>
              </CardHeader>
              <CardContent className="space-y-4 max-w-md">
                <div className="space-y-2">
                  <Label className="text-foreground">Senha Atual</Label>
                  <Input
                    type="password"
                    value={passwords.current}
                    onChange={(e) => setPasswords({ ...passwords, current: e.target.value })}
                    className="bg-background border-input text-foreground"
                  />
                </div>
                <div className="space-y-2">
                  <Label className="text-foreground">Nova Senha</Label>
                  <Input
                    type="password"
                    value={passwords.new}
                    onChange={(e) => setPasswords({ ...passwords, new: e.target.value })}
                    className="bg-background border-input text-foreground"
                  />
                </div>
                <div className="space-y-2">
                  <Label className="text-foreground">Confirmar Nova Senha</Label>
                  <Input
                    type="password"
                    value={passwords.confirm}
                    onChange={(e) => setPasswords({ ...passwords, confirm: e.target.value })}
                    className="bg-background border-input text-foreground"
                  />
                </div>
                <div className="pt-4">
                  <Button
                    onClick={handleChangePassword}
                    disabled={isSaving}
                    className="w-full bg-primary hover:bg-primary/90 text-primary-foreground"
                  >
                    {isSaving ? (
                      <Loader2 className="h-4 w-4 animate-spin mr-2" />
                    ) : (
                      <Lock className="h-4 w-4 mr-2" />
                    )}{' '}
                    Atualizar Senha
                  </Button>
                </div>
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="attendance" className="mt-6 space-y-6">
            <AttendanceSlaSettings />
            <CompanyAttendanceSettings />
          </TabsContent>
        </Tabs>
      </div>
    </PageShell>
  );
}
