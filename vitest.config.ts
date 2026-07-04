import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

/**
 * S6 — Test runner JS (vitest) para os testes de regras puras de atendimento.
 *
 * Inclui o CASO OBRIGATÓRIO §9.3/§18.1 (deep-merge não apaga csv_analytics) e os
 * demais testes de mapStatusToAction/validateMessageType/normalizePhone, que antes
 * só passavam no typecheck (sem runner). Resolve o alias `@/` do tsconfig sem
 * depender de plugin extra.
 *
 * §18.1/§18.3 (render): o ambiente PADRÃO é `node` (testes de lógica pura). Os
 * testes de SMOKE-RENDER de componentes/hooks (.test.tsx) optam por `jsdom`
 * VIA DIRETIVA por-arquivo no topo do arquivo:
 *     // @vitest-environment jsdom
 * Assim os testes node existentes não pagam o custo do DOM e os de render têm
 * `document`/`window`. O `setupFiles` carrega os matchers do jest-dom só quando
 * há DOM (no-op em node).
 */
export default defineConfig({
  test: {
    globals: true,
    environment: 'node',
    include: [
      '__tests__/**/*.test.ts',
      '__tests__/**/*.test.tsx',
      'lib/**/*.test.ts',
    ],
    setupFiles: ['./__tests__/setup/testing-library.ts'],
  },
  // JSX automático (React 17+): esbuild injeta o runtime, então os .tsx de render
  // não precisam de `import React`. Inócuo para os testes .ts (sem JSX).
  esbuild: {
    jsx: 'automatic',
  },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./', import.meta.url)),
    },
  },
});
