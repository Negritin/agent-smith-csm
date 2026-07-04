'use client';

import * as React from 'react';
import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Checkbox } from '@/components/ui/checkbox';
import { AuthCard, AuthShell } from '@/components/auth/auth-shell';
import { PasswordField } from '@/components/auth/password-field';
import { InlineNotice } from '@/components/ui/feedback-state';

export default function LoginPage() {
  const router = useRouter();
  const [formData, setFormData] = useState({
    email: '',
    password: '',
    rememberMe: false,
  });
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setIsLoading(true);

    try {
      const response = await fetch('/api/auth/login', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(formData),
      });

      const data = await response.json();

      if (!response.ok) {
        setError(data.error || 'Erro ao fazer login');
        setIsLoading(false);
        return;
      }

      // BAIXO-002: a sessao e mantida no cookie iron-session httpOnly definido
      // pela rota /api/auth/login; nada de PII e persistido em localStorage.

      router.push('/dashboard/chat');
    } catch (err) {
      console.error('Login error:', err);
      setError('Erro ao conectar com o servidor');
      setIsLoading(false);
    }
  };

  return (
    <AuthShell>
      <AuthCard
        title="Bem-vindo de volta"
        description="Entre para acessar sua conta"
        footer={
          <p className="w-full text-center text-sm text-muted-foreground">
            Novo por aqui?{' '}
            <a href="/register" className="font-medium text-primary hover:underline">
              Criar conta
            </a>
          </p>
        }
      >
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="email">Email</Label>
            <Input
              id="email"
              type="email"
              value={formData.email}
              onChange={(e) => setFormData({ ...formData, email: e.target.value })}
              placeholder="seu@email.com"
              required
            />
          </div>

          <PasswordField
            id="password"
            label="Senha"
            value={formData.password}
            onChange={(e) => setFormData({ ...formData, password: e.target.value })}
            placeholder="Digite sua senha"
            autoComplete="current-password"
            required
          />

          {error && <InlineNotice tone="danger">{error}</InlineNotice>}

          <div className="flex items-center justify-between">
            <div className="flex items-center space-x-2">
              <Checkbox
                id="remember"
                checked={formData.rememberMe}
                onCheckedChange={(checked) =>
                  setFormData({ ...formData, rememberMe: checked as boolean })
                }
                className="border-border"
              />
              <label htmlFor="remember" className="text-sm text-muted-foreground">
                Lembrar-me
              </label>
            </div>
            <a href="/forgot-password" className="text-sm text-primary hover:underline">
              Esqueceu a senha?
            </a>
          </div>

          <Button type="submit" disabled={isLoading} className="w-full">
            {isLoading ? 'Entrando...' : 'Entrar'}
          </Button>
        </form>
      </AuthCard>
    </AuthShell>
  );
}
