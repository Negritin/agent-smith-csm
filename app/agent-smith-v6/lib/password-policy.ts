/**
 * Política de senha da plataforma — FONTE ÚNICA.
 *
 * Módulo PURO e sem side effects (não importa Supabase/bcrypt nem lê env no
 * top-level), de propósito: pode ser importado tanto no servidor (API routes,
 * server actions) quanto em componentes `'use client'` — sem duplicar a regra e
 * sem arrastar dependências de servidor pro bundle do cliente.
 *
 * Requisitos: 8+ caracteres, 1 maiúscula, 1 minúscula, 1 número.
 */

export const PASSWORD_MIN_LENGTH = 8;

export interface PasswordValidationResult {
  valid: boolean;
  errors: string[];
}

export function validatePasswordStrength(password: string): PasswordValidationResult {
  const errors: string[] = [];

  if (!password || password.length < PASSWORD_MIN_LENGTH) {
    errors.push(`Senha deve ter pelo menos ${PASSWORD_MIN_LENGTH} caracteres`);
  }

  if (!/[A-Z]/.test(password)) {
    errors.push('Senha deve conter pelo menos 1 letra maiúscula');
  }

  if (!/[a-z]/.test(password)) {
    errors.push('Senha deve conter pelo menos 1 letra minúscula');
  }

  if (!/[0-9]/.test(password)) {
    errors.push('Senha deve conter pelo menos 1 número');
  }

  return {
    valid: errors.length === 0,
    errors,
  };
}
