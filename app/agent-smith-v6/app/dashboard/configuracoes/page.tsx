'use client';

import { useState, useEffect } from 'react';
import { useUserId } from '@/hooks/useUserId';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { toast } from 'sonner';
import { Loader2, Lock, User, Save } from 'lucide-react';
import { AvatarUpload } from '@/components/AvatarUpload';
import { PageDescription, PageHeader, PageShell, PageTitle } from '@/components/ui/page-shell';

export default function SettingsPage() {
  const { userId } = useUserId();
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);

  // Profile State
  const [profile, setProfile] = useState({
    first_name: '',
    last_name: '',
    email: '',
    phone: '',
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

  useEffect(() => {
    if (userId) loadProfile();
  }, [userId]);

  const loadProfile = async () => {
    try {
      const response = await fetch('/api/user/profile?full=true');
      if (!response.ok) throw new Error('Failed to load profile');

      const data = await response.json();
      setProfile({
        first_name: data.first_name || '',
        last_name: data.last_name || '',
        email: data.email || '',
        phone: data.phone || '',
        cpf: data.cpf || '',
        birth_date: data.birth_date || '',
        avatar_url: data.avatar_url || '',
      });
    } catch (error) {
      console.error('Erro ao carregar perfil:', error);
      toast.error('Erro ao carregar dados.');
    } finally {
      setIsLoading(false);
    }
  };

  const handleUpdateProfile = async () => {
    setIsSaving(true);
    try {
      const response = await fetch('/api/user/profile', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          first_name: profile.first_name,
          last_name: profile.last_name,
          phone: profile.phone,
          avatar_url: profile.avatar_url,
        }),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || 'Failed to update');
      }

      toast.success('Cadastro salvo com sucesso!');
    } catch (error: any) {
      console.error('Erro ao atualizar:', error);
      toast.error(error.message || 'Falha ao atualizar perfil.');
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
      const response = await fetch('/api/auth/change-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          userId: userId,
          currentPassword: passwords.current,
          newPassword: passwords.new,
        }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || 'Erro ao alterar senha');
      }

      toast.success('Troca de senha realizada com sucesso!');
      setPasswords({ current: '', new: '', confirm: '' });
    } catch (error: any) {
      console.error('Erro ao mudar senha:', error);
      toast.error(error.message || 'Erro ao alterar senha.');
    } finally {
      setIsSaving(false);
    }
  };

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center bg-background text-muted-foreground">
        <Loader2 className="h-8 w-8 animate-spin mr-2" /> Carregando...
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background text-foreground">
      <div className="overflow-y-auto h-screen bg-background">
        <PageShell size="default">
          <PageHeader>
            <div>
              <PageTitle>Configurações</PageTitle>
              <PageDescription>Gerencie seus dados pessoais e segurança.</PageDescription>
            </div>
          </PageHeader>

          <Tabs defaultValue="profile" className="w-full">
            <TabsList className="grid w-full grid-cols-2 bg-muted border border-border">
              <TabsTrigger
                value="profile"
                className="data-[state=active]:bg-background data-[state=active]:text-foreground data-[state=active]:shadow-sm"
              >
                <User className="h-4 w-4 mr-2" /> Dados Pessoais
              </TabsTrigger>
              <TabsTrigger
                value="security"
                className="data-[state=active]:bg-background data-[state=active]:text-foreground data-[state=active]:shadow-sm"
              >
                <Lock className="h-4 w-4 mr-2" /> Senha e Segurança
              </TabsTrigger>
            </TabsList>

            <TabsContent value="profile" className="mt-6">
              <Card className="bg-card border-border">
                <CardHeader>
                  <CardTitle className="text-foreground">Suas Informações</CardTitle>
                  <CardDescription className="text-muted-foreground">
                    Dados cadastrais. Campos sensíveis são bloqueados para edição.
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-6">
                  {/* Avatar Upload */}
                  <div className="flex items-center gap-6">
                    {userId && (
                      <AvatarUpload
                        currentImageUrl={profile.avatar_url}
                        onUpload={(url) => setProfile({ ...profile, avatar_url: url })}
                        uploadPath="users"
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
                  <div className="space-y-2">
                    <Label className="text-foreground">Telefone / WhatsApp</Label>
                    <Input
                      value={profile.phone}
                      onChange={(e) => setProfile({ ...profile, phone: e.target.value })}
                      className="bg-background border-input text-foreground"
                    />
                  </div>
                  <div className="grid grid-cols-3 gap-4 pt-4">
                    <div className="space-y-2 opacity-70">
                      <Label className="text-foreground">Email</Label>
                      <Input
                        value={profile.email}
                        disabled
                        className="bg-muted border-border text-muted-foreground"
                      />
                    </div>
                    <div className="space-y-2 opacity-70">
                      <Label className="text-foreground">CPF</Label>
                      <Input
                        value={profile.cpf}
                        disabled
                        className="bg-muted border-border text-muted-foreground"
                      />
                    </div>
                    <div className="space-y-2 opacity-70">
                      <Label className="text-foreground">Data de Nascimento</Label>
                      <Input
                        type="date"
                        value={profile.birth_date}
                        disabled
                        className="bg-muted border-border text-muted-foreground block w-full"
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
                      Salvar Dados
                    </Button>
                  </div>
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="security" className="mt-6">
              <Card className="bg-card border-border">
                <CardHeader>
                  <CardTitle className="text-foreground">Alterar Senha</CardTitle>
                  <CardDescription className="text-muted-foreground">
                    Para sua segurança, confirme a senha atual.
                  </CardDescription>
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
          </Tabs>
        </PageShell>
      </div>
    </div>
  );
}
