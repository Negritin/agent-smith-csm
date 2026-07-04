'use client';

import * as React from 'react';
import { useState, useEffect, Suspense } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { ArrowLeft, CheckCircle, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { AuthCard, AuthShell } from '@/components/auth/auth-shell';
import { PasswordField } from '@/components/auth/password-field';
import { InlineNotice } from '@/components/ui/feedback-state';
import { validatePasswordStrength } from '@/lib/password-policy';

function ResetPasswordContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const emailFromUrl = searchParams.get('email') || '';

  const [email, setEmail] = useState(emailFromUrl);
  const [code, setCode] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState(false);
  const [userType, setUserType] = useState<'admin' | 'member' | null>(null);

  useEffect(() => {
    if (emailFromUrl) {
      setEmail(emailFromUrl);
    }
  }, [emailFromUrl]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');

    // Validations
    if (!email || !code || !newPassword || !confirmPassword) {
      setError('Preencha todos os campos');
      return;
    }

    if (newPassword !== confirmPassword) {
      setError('As senhas não conferem');
      return;
    }

    const passwordCheck = validatePasswordStrength(newPassword);
    if (!passwordCheck.valid) {
      setError(passwordCheck.errors[0]);
      return;
    }

    setIsLoading(true);

    try {
      const response = await fetch('/api/auth/reset-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, code, newPassword }),
      });

      const data = await response.json();

      if (!response.ok) {
        setError(data.error || 'Erro ao redefinir senha');
        setIsLoading(false);
        return;
      }

      setSuccess(true);
      setUserType(data.userType || 'member');

      // Redirect based on user type after 3 seconds
      setTimeout(() => {
        if (data.userType === 'admin') {
          router.push('/admin/login');
        } else {
          router.push('/login');
        }
      }, 3000);
    } catch (err) {
      console.error('Reset password error:', err);
      setError('Erro ao conectar com o servidor');
      setIsLoading(false);
    }
  };

  return (
    <AuthShell>
      <AuthCard
        title="Redefinir senha"
        description="Digite o código recebido por email e sua nova senha"
        logoHref="/login"
      >
        {success ? (
          <div className="text-center py-6">
            <div className="w-16 h-16 bg-success/10 rounded-full flex items-center justify-center mx-auto mb-4">
              <CheckCircle className="w-8 h-8 text-success" />
            </div>
            <h3 className="text-lg font-medium text-success mb-2">Senha Alterada!</h3>
            <p className="text-muted-foreground text-sm">Sua senha foi redefinida com sucesso.</p>
            <p className="text-muted-foreground text-xs mt-4">
              Redirecionando para o login {userType === 'admin' ? 'administrativo' : ''}...
            </p>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="space-y-4">
            {error && <InlineNotice tone="danger">{error}</InlineNotice>}

            <div className="space-y-2">
              <Label htmlFor="email">Email</Label>
              <Input
                id="email"
                type="email"
                placeholder="seu@email.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="code">Código de Verificação</Label>
              <Input
                id="code"
                type="text"
                placeholder="Cole o código de 32 caracteres"
                value={code}
                onChange={(e) =>
                  setCode(
                    e.target.value
                      .toUpperCase()
                      .replace(/[^A-Z0-9]/g, '')
                      .slice(0, 32),
                  )
                }
                required
                maxLength={32}
                autoComplete="one-time-code"
                className="text-center text-sm tracking-wide font-mono"
              />
            </div>

            <PasswordField
              id="newPassword"
              label="Nova senha"
              placeholder="Mín. 8 caracteres, com maiúscula, minúscula e número"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              autoComplete="new-password"
              required
            />

            <PasswordField
              id="confirmPassword"
              label="Confirmar nova senha"
              placeholder="Repita a nova senha"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              autoComplete="new-password"
              required
            />

            <Button
              type="submit"
              disabled={isLoading || !email || !code || !newPassword || !confirmPassword}
              variant="default"
              className="w-full"
            >
              {isLoading ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  Redefinindo...
                </>
              ) : (
                'Redefinir Senha'
              )}
            </Button>

            <div className="flex justify-between mt-4">
              <button
                type="button"
                onClick={() => router.push('/forgot-password')}
                className="text-sm text-muted-foreground hover:text-foreground transition-colors"
              >
                Reenviar código
              </button>
              <button
                type="button"
                onClick={() => router.push('/login')}
                className="flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground transition-colors"
              >
                <ArrowLeft className="w-4 h-4" />
                Voltar para o login
              </button>
            </div>
          </form>
        )}
      </AuthCard>
    </AuthShell>
  );
}

export default function ResetPasswordPage() {
  return (
    <Suspense
      fallback={
        <div className="flex items-center justify-center min-h-screen bg-background">
          <Loader2 className="w-8 h-8 animate-spin text-primary" />
        </div>
      }
    >
      <ResetPasswordContent />
    </Suspense>
  );
}
