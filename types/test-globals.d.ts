/**
 * S6 — Shim de tipos para os testes do frontend (rodam com vitest).
 *
 * O runner JS é o vitest (`npm test` -> `vitest run`, ver package.json + vitest.config.ts),
 * que injeta os globais `describe/it/expect` em runtime (`globals: true`). Este shim
 * declara o subconjunto desses globais APENAS para o `tsc --noEmit` (typecheck) não
 * quebrar sem precisar carregar `vitest/globals` no tsconfig `types` (o que afetaria
 * todo o projeto). Os testes NÃO importam vitest explicitamente, então não há conflito
 * de declarações. Caso futuramente se opte por `types: ["vitest/globals"]` no tsconfig,
 * este arquivo deve ser removido para evitar declarações duplicadas.
 */

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type _TestExpect = (actual: any) => {
  toBe(expected: unknown): void;
  toEqual(expected: unknown): void;
  toBeNull(): void;
  toBeTruthy(): void;
  toBeFalsy(): void;
  toHaveProperty(key: string, value?: unknown): void;
  not: {
    toBe(expected: unknown): void;
    toHaveProperty(key: string): void;
  };
};

declare function describe(name: string, fn: () => void): void;
declare function it(name: string, fn: () => void | Promise<void>): void;
declare function test(name: string, fn: () => void | Promise<void>): void;
declare const expect: _TestExpect;
