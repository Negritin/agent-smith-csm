'use client';

import * as React from 'react';
import { useState, useEffect, Suspense } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { CheckCircle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Checkbox } from '@/components/ui/checkbox';
import { AuthCard, AuthShell } from '@/components/auth/auth-shell';
import { PasswordField } from '@/components/auth/password-field';
import { InlineNotice } from '@/components/ui/feedback-state';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

function formatCPF(value: string): string {
  const numbers = value.replace(/\D/g, '');
  if (numbers.length <= 3) return numbers;
  if (numbers.length <= 6) return `${numbers.slice(0, 3)}.${numbers.slice(3)}`;
  if (numbers.length <= 9)
    return `${numbers.slice(0, 3)}.${numbers.slice(3, 6)}.${numbers.slice(6)}`;
  return `${numbers.slice(0, 3)}.${numbers.slice(3, 6)}.${numbers.slice(6, 9)}-${numbers.slice(9, 11)}`;
}

function formatPhone(value: string): string {
  const numbers = value.replace(/\D/g, '');
  if (numbers.length <= 2) return numbers;
  if (numbers.length <= 7) return `(${numbers.slice(0, 2)}) ${numbers.slice(2)}`;
  if (numbers.length <= 11)
    return `(${numbers.slice(0, 2)}) ${numbers.slice(2, 7)}-${numbers.slice(7)}`;
  return `(${numbers.slice(0, 2)}) ${numbers.slice(2, 7)}-${numbers.slice(7, 11)}`;
}

function formatDate(value: string): string {
  const numbers = value.replace(/\D/g, '');
  if (numbers.length <= 2) return numbers;
  if (numbers.length <= 4) return `${numbers.slice(0, 2)}/${numbers.slice(2)}`;
  return `${numbers.slice(0, 2)}/${numbers.slice(2, 4)}/${numbers.slice(4, 8)}`;
}

function isValidCPF(cpf: string): boolean {
  const numbers = cpf.replace(/\D/g, '');
  if (numbers.length !== 11) return false;
  if (/^(\d)\1+$/.test(numbers)) return false;

  let sum = 0;
  for (let i = 0; i < 9; i++) {
    sum += parseInt(numbers.charAt(i)) * (10 - i);
  }
  let digit = 11 - (sum % 11);
  if (digit >= 10) digit = 0;
  if (digit !== parseInt(numbers.charAt(9))) return false;

  sum = 0;
  for (let i = 0; i < 10; i++) {
    sum += parseInt(numbers.charAt(i)) * (11 - i);
  }
  digit = 11 - (sum % 11);
  if (digit >= 10) digit = 0;
  if (digit !== parseInt(numbers.charAt(10))) return false;

  return true;
}

function isValidEmail(email: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
}

function isValidPhone(phone: string): boolean {
  const numbers = phone.replace(/\D/g, '');
  return numbers.length === 11 || numbers.length === 10;
}

function isValidDate(date: string): boolean {
  const numbers = date.replace(/\D/g, '');
  if (numbers.length !== 8) return false;

  const day = parseInt(numbers.slice(0, 2));
  const month = parseInt(numbers.slice(2, 4));
  const year = parseInt(numbers.slice(4, 8));

  if (month < 1 || month > 12) return false;
  if (day < 1 || day > 31) return false;
  if (year < 1900 || year > new Date().getFullYear()) return false;

  const dateObj = new Date(year, month - 1, day);
  return (
    dateObj.getDate() === day && dateObj.getMonth() === month - 1 && dateObj.getFullYear() === year
  );
}

function RegisterPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [isLoading, setIsLoading] = useState(false);
  const [submitError, setSubmitError] = useState('');
  const [inviteToken, setInviteToken] = useState<string | null>(null);
  const [inviteData, setInviteData] = useState<{
    companyName: string;
    companyId: string;
    email?: string | null;
  } | null>(null);
  const [validatingInvite, setValidatingInvite] = useState(false);

  // Legal documents state
  const [termsModalOpen, setTermsModalOpen] = useState(false);
  const [termsModalType, setTermsModalType] = useState<'terms_of_use' | 'privacy_policy'>(
    'terms_of_use',
  );
  const [legalDocs, setLegalDocs] = useState<{
    terms_of_use: { id: string; title: string; content: string; version: string } | null;
    privacy_policy: { id: string; title: string; content: string; version: string } | null;
  }>({ terms_of_use: null, privacy_policy: null });

  const [formData, setFormData] = useState({
    firstName: '',
    lastName: '',
    cpf: '',
    phone: '',
    email: '',
    birthDate: '',
    password: '',
    termsAccepted: false,
  });

  const [errors, setErrors] = useState({
    firstName: '',
    lastName: '',
    cpf: '',
    phone: '',
    email: '',
    birthDate: '',
    password: '',
    termsAccepted: '',
  });

  const [touched, setTouched] = useState({
    firstName: false,
    lastName: false,
    cpf: false,
    phone: false,
    email: false,
    birthDate: false,
    password: false,
  });

  // Validate invite token on mount
  useEffect(() => {
    const token = searchParams?.get('token');
    if (token) {
      setInviteToken(token);
      validateInviteToken(token);
    }
  }, [searchParams]);

  // Fetch active legal documents on mount
  useEffect(() => {
    const fetchLegalDocs = async () => {
      try {
        const [termsRes, privacyRes] = await Promise.all([
          fetch('/api/legal-documents/active?type=terms_of_use'),
          fetch('/api/legal-documents/active?type=privacy_policy'),
        ]);
        if (termsRes.ok) {
          const termsData = await termsRes.json();
          if (termsData.document) {
            setLegalDocs((prev) => ({ ...prev, terms_of_use: termsData.document }));
          }
        }
        if (privacyRes.ok) {
          const privacyData = await privacyRes.json();
          if (privacyData.document) {
            setLegalDocs((prev) => ({ ...prev, privacy_policy: privacyData.document }));
          }
        }
      } catch (error) {
        console.error('Error fetching legal docs:', error);
      }
    };
    fetchLegalDocs();
  }, []);

  const validateInviteToken = async (token: string) => {
    setValidatingInvite(true);
    try {
      const response = await fetch('/api/invites/validate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token }),
      });

      const data = await response.json();

      if (response.ok && data.valid) {
        setInviteData({
          companyName: data.companyName,
          companyId: data.companyId,
          email: data.email,
        });

        // Pre-fill email if provided in invite
        if (data.email) {
          setFormData({ ...formData, email: data.email });
        }
      } else {
        setSubmitError(data.error || 'Token de convite inválido');
        setInviteToken(null);
      }
    } catch (error) {
      console.error('Error validating invite:', error);
      setSubmitError('Erro ao validar convite');
      setInviteToken(null);
    } finally {
      setValidatingInvite(false);
    }
  };

  const handleCPFChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const formatted = formatCPF(e.target.value);
    setFormData({ ...formData, cpf: formatted });
  };

  const handlePhoneChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const formatted = formatPhone(e.target.value);
    setFormData({ ...formData, phone: formatted });
  };

  const handleDateChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const formatted = formatDate(e.target.value);
    setFormData({ ...formData, birthDate: formatted });
  };

  const validateField = (field: string, value: string | boolean) => {
    let error = '';

    switch (field) {
      case 'firstName':
      case 'lastName':
        if (!value || (typeof value === 'string' && value.trim() === '')) {
          error = 'Campo obrigatório';
        }
        break;
      case 'cpf':
        if (!value || (typeof value === 'string' && value.trim() === '')) {
          error = 'Campo obrigatório';
        } else if (typeof value === 'string' && !isValidCPF(value)) {
          error = 'CPF inválido';
        }
        break;
      case 'phone':
        if (!value || (typeof value === 'string' && value.trim() === '')) {
          error = 'Campo obrigatório';
        } else if (typeof value === 'string' && !isValidPhone(value)) {
          error = 'Telefone inválido';
        }
        break;
      case 'email':
        if (!value || (typeof value === 'string' && value.trim() === '')) {
          error = 'Campo obrigatório';
        } else if (typeof value === 'string' && !isValidEmail(value)) {
          error = 'Email inválido';
        }
        break;
      case 'birthDate':
        if (!value || (typeof value === 'string' && value.trim() === '')) {
          error = 'Campo obrigatório';
        } else if (typeof value === 'string' && !isValidDate(value)) {
          error = 'Data inválida';
        }
        break;
      case 'password':
        if (!value || (typeof value === 'string' && value.trim() === '')) {
          error = 'Campo obrigatório';
        } else if (typeof value === 'string' && value.length < 6) {
          error = 'Senha deve ter no mínimo 6 caracteres';
        }
        break;
    }

    setErrors({ ...errors, [field]: error });
  };

  const handleBlur = (field: string) => {
    setTouched({ ...touched, [field]: true });
    validateField(field, formData[field as keyof typeof formData]);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitError('');

    const allTouched = {
      firstName: true,
      lastName: true,
      cpf: true,
      phone: true,
      email: true,
      birthDate: true,
      password: true,
    };
    setTouched(allTouched);

    Object.keys(formData).forEach((key) => {
      validateField(key, formData[key as keyof typeof formData]);
    });

    if (!formData.termsAccepted) {
      setErrors({ ...errors, termsAccepted: 'Você deve aceitar os termos e condições' });
      return;
    }

    const hasErrors = Object.values(errors).some((error) => error !== '');
    if (hasErrors) {
      return;
    }

    setIsLoading(true);

    try {
      const payload = {
        ...formData,
        inviteToken: inviteToken || undefined,
        acceptedTermsVersion: legalDocs.terms_of_use?.id || undefined,
      };

      const response = await fetch('/api/auth/signup', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
      });

      const data = await response.json();

      if (!response.ok) {
        setSubmitError(data.error || 'Erro ao criar conta');
        setIsLoading(false);
        return;
      }

      // Se cadastrou via invite, mostra mensagem de pendente
      if (inviteToken) {
        router.push('/pending-approval');
      } else {
        router.push('/dashboard');
      }
    } catch (error) {
      console.error('Signup error:', error);
      setSubmitError('Erro ao conectar com o servidor');
      setIsLoading(false);
    }
  };

  return (
    <AuthShell size="lg">
      <AuthCard
        title="Criar conta"
        description={
          inviteData
            ? `Você foi convidado para ${inviteData.companyName}`
            : 'Bem-vindo! Crie uma conta para começar.'
        }
        contentClassName="px-6 md:px-8"
        headerAction={
          <div className="w-full space-y-2">
            {inviteData && (
              <InlineNotice tone="success" className="flex items-center gap-2 text-left">
                <CheckCircle className="h-4 w-4 flex-shrink-0" />
                <span>
                  Convite válido para{' '}
                  <span className="font-semibold">{inviteData.companyName}</span>
                </span>
              </InlineNotice>
            )}
            {validatingInvite && <InlineNotice tone="brand">Validando convite...</InlineNotice>}
          </div>
        }
        footer={
          <p className="w-full text-center text-sm text-muted-foreground">
            Já tem uma conta?{' '}
            <a href="/login" className="font-medium text-primary hover:underline">
              Entrar
            </a>
          </p>
        }
      >
        <form onSubmit={handleSubmit}>
          <div className="mb-4 grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="firstName">Nome</Label>
              <Input
                id="firstName"
                value={formData.firstName}
                onChange={(e) => setFormData({ ...formData, firstName: e.target.value })}
                onBlur={() => handleBlur('firstName')}
                className={`${touched.firstName && errors.firstName ? 'border-danger' : ''}`}
              />
              {touched.firstName && errors.firstName && (
                <p className="text-danger text-xs">{errors.firstName}</p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="lastName">Sobrenome</Label>
              <Input
                id="lastName"
                value={formData.lastName}
                onChange={(e) => setFormData({ ...formData, lastName: e.target.value })}
                onBlur={() => handleBlur('lastName')}
                className={`${touched.lastName && errors.lastName ? 'border-danger' : ''}`}
              />
              {touched.lastName && errors.lastName && (
                <p className="text-danger text-xs">{errors.lastName}</p>
              )}
            </div>
          </div>

          <div className="space-y-2 mb-4">
            <Label htmlFor="cpf">CPF</Label>
            <Input
              id="cpf"
              value={formData.cpf}
              onChange={handleCPFChange}
              onBlur={() => handleBlur('cpf')}
              placeholder="000.000.000-00"
              maxLength={14}
              className={`${touched.cpf && errors.cpf ? 'border-danger' : ''}`}
            />
            {touched.cpf && errors.cpf && <p className="text-danger text-xs">{errors.cpf}</p>}
          </div>

          <div className="space-y-2 mb-4">
            <Label htmlFor="phone">Telefone</Label>
            <Input
              id="phone"
              value={formData.phone}
              onChange={handlePhoneChange}
              onBlur={() => handleBlur('phone')}
              placeholder="(00) 00000-0000"
              maxLength={15}
              className={`${touched.phone && errors.phone ? 'border-danger' : ''}`}
            />
            {touched.phone && errors.phone && <p className="text-danger text-xs">{errors.phone}</p>}
          </div>

          <div className="space-y-2 mb-4">
            <Label htmlFor="email">Email</Label>
            <Input
              id="email"
              type="email"
              value={formData.email}
              onChange={(e) => setFormData({ ...formData, email: e.target.value })}
              onBlur={() => handleBlur('email')}
              disabled={!!inviteData?.email}
              className={`${touched.email && errors.email ? 'border-danger' : ''} ${
                inviteData?.email ? 'opacity-75 cursor-not-allowed' : ''
              }`}
            />
            {inviteData?.email && (
              <p className="text-xs text-success flex items-center gap-1">
                <CheckCircle className="w-3 h-3" />
                Email do convite
              </p>
            )}
            {touched.email && errors.email && <p className="text-danger text-xs">{errors.email}</p>}
          </div>

          <div className="space-y-2 mb-4">
            <Label htmlFor="birthDate">Data de Nascimento</Label>
            <Input
              id="birthDate"
              value={formData.birthDate}
              onChange={handleDateChange}
              onBlur={() => handleBlur('birthDate')}
              placeholder="DD/MM/AAAA"
              maxLength={10}
              className={`${touched.birthDate && errors.birthDate ? 'border-danger' : ''}`}
            />
            {touched.birthDate && errors.birthDate && (
              <p className="text-danger text-xs">{errors.birthDate}</p>
            )}
          </div>

          <PasswordField
            id="password"
            label="Senha"
            value={formData.password}
            onChange={(e) => setFormData({ ...formData, password: e.target.value })}
            onBlur={() => handleBlur('password')}
            error={touched.password ? errors.password : ''}
            autoComplete="new-password"
            fieldClassName="mb-4"
          />

          <div className="flex items-center space-x-2 mb-6">
            <Checkbox
              id="terms"
              checked={formData.termsAccepted}
              onCheckedChange={(checked) =>
                setFormData({ ...formData, termsAccepted: checked as boolean })
              }
              className="border-border"
            />
            <label htmlFor="terms" className="text-sm text-muted-foreground">
              Eu concordo com os{' '}
              <button
                type="button"
                className="text-primary hover:underline cursor-pointer"
                onClick={() => {
                  setTermsModalType('terms_of_use');
                  setTermsModalOpen(true);
                }}
              >
                Termos de Uso
              </button>{' '}
              e{' '}
              <button
                type="button"
                className="text-primary hover:underline cursor-pointer"
                onClick={() => {
                  setTermsModalType('privacy_policy');
                  setTermsModalOpen(true);
                }}
              >
                Política de Privacidade
              </button>
            </label>
          </div>
          {errors.termsAccepted && (
            <p className="text-danger text-xs mb-4">{errors.termsAccepted}</p>
          )}

          {submitError && (
            <InlineNotice tone="danger" className="mb-4">
              {submitError}
            </InlineNotice>
          )}

          <Button type="submit" disabled={isLoading || validatingInvite} className="w-full">
            {isLoading ? 'Criando conta...' : 'Criar conta gratuita'}
          </Button>
        </form>
      </AuthCard>

      {/* Legal Document Modal */}
      <Dialog open={termsModalOpen} onOpenChange={setTermsModalOpen}>
        <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="text-xl">
              {termsModalType === 'terms_of_use' ? 'Termos de Uso' : 'Política de Privacidade'}
            </DialogTitle>
            <DialogDescription className="sr-only">Conteúdo do documento legal</DialogDescription>
          </DialogHeader>
          <div className="py-4">
            {legalDocs[termsModalType] ? (
              <div className="bg-muted rounded-lg p-6 border border-border whitespace-pre-wrap text-foreground text-sm leading-relaxed max-h-[50vh] overflow-y-auto">
                {legalDocs[termsModalType]!.content}
              </div>
            ) : (
              <p className="text-muted-foreground text-center py-8">
                Documento não disponível no momento.
              </p>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </AuthShell>
  );
}

export default function RegisterPage() {
  return (
    <Suspense
      fallback={
        <div className="flex items-center justify-center min-h-screen bg-background text-foreground">
          Carregando...
        </div>
      }
    >
      <RegisterPageContent />
    </Suspense>
  );
}
