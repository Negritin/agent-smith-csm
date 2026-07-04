'use client';

import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Shield } from 'lucide-react';
import { AuthCard, AuthShell } from '@/components/auth/auth-shell';
import { PasswordField } from '@/components/auth/password-field';
import { InlineNotice } from '@/components/ui/feedback-state';

export default function AdminLoginPage() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      const response = await fetch('/api/admin/login', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ email, password }),
      });

      const data = await response.json();

      if (!response.ok) {
        setError(data.error || 'Erro ao fazer login');
        setLoading(false);
        return;
      }

      // Força recarregamento real da página para garantir envio dos cookies
      window.location.href = '/admin';
    } catch (err) {
      console.error('[ADMIN LOGIN] Login error:', err);
      setError('Erro ao processar login');
    } finally {
      setLoading(false);
    }
  };

  return (
    <AuthShell>
      <AuthCard
        title="Painel Administrativo"
        description="Acesso exclusivo para administradores autorizados"
        logoHref="/landing"
        headerAction={
          <div className="flex items-center gap-2 rounded-md border border-primary/20 bg-brand-muted px-3 py-2 text-sm font-medium text-primary">
            <Shield className="h-4 w-4" />
            Agent Smith v7.0
          </div>
        }
        footer={
          <p className="w-full text-center text-xs text-muted-foreground">
            Área restrita. Use apenas credenciais administrativas.
          </p>
        }
      >
        {error && (
          <InlineNotice tone="danger" className="mb-4">
            {error}
          </InlineNotice>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="email">Email</Label>
            <Input
              id="email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="admin@exemplo.com"
              required
              disabled={loading}
            />
          </div>

          <PasswordField
            id="password"
            label="Senha"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Digite sua senha"
            required
            disabled={loading}
            autoComplete="current-password"
          />

          <div className="flex justify-end">
            <a href="/forgot-password" className="text-sm text-primary hover:underline">
              Esqueceu a senha?
            </a>
          </div>

          <Button type="submit" disabled={loading} className="w-full">
            {loading ? 'Entrando...' : 'Entrar como Admin'}
          </Button>
        </form>
      </AuthCard>
    </AuthShell>
  );
}
