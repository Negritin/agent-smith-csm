/**
 * Setup global do vitest (§18.1/§18.3).
 *
 * Registra os matchers do @testing-library/jest-dom (toBeInTheDocument, etc.) via
 * `expect.extend` e faz cleanup automático entre testes de render.
 *
 * Importar estes módulos é seguro mesmo nos testes com `environment: 'node'` (a
 * maioria — lógica pura): jest-dom só estende o `expect` e o `cleanup` do RTL só
 * TOCA o DOM quando chamado. Por isso o `afterEach(cleanup)` é GUARDADO por
 * detecção de DOM — em node vira no-op e não quebra nem custa.
 */
import { afterEach } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';

const hasDom = typeof document !== 'undefined' && typeof window !== 'undefined';

afterEach(() => {
  if (hasDom) cleanup();
});
