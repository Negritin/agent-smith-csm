/**
 * ALTO-005 — Testes da allowlist centralizada de origem do widget.
 *
 * Caracterizam o comportamento (preservado no refactor de deduplicação) das
 * funções puras `isOriginValueAllowed`, `parseAllowedDomain`, `parseOrigin` e
 * `extractAllowedDomains`, com foco nos eixos exigidos: wildcard, IDN e porta.
 */
import { describe, expect, it } from 'vitest';

import {
  extractAllowedDomains,
  isOriginValueAllowed,
  parseAllowedDomain,
  parseOrigin,
} from '@/lib/security/widget-origin';

describe('extractAllowedDomains', () => {
  it('lê array de strings preservando apenas itens string', () => {
    expect(extractAllowedDomains({ allowedDomains: ['a.com', 1, 'b.com', null] })).toEqual([
      'a.com',
      'b.com',
    ]);
  });

  it('lê string separada por vírgula/quebra de linha', () => {
    expect(extractAllowedDomains({ allowed_domains: 'a.com, b.com\n c.com ' })).toEqual([
      'a.com',
      'b.com',
      'c.com',
    ]);
  });

  it('retorna vazio quando config ausente ou sem chaves conhecidas', () => {
    expect(extractAllowedDomains(null)).toEqual([]);
    expect(extractAllowedDomains({ foo: 'bar' })).toEqual([]);
  });
});

describe('isOriginValueAllowed — match exato de host', () => {
  const config = { allowedDomains: ['example.com'] };

  it('aceita o host idêntico ignorando o protocolo quando não especificado', () => {
    expect(isOriginValueAllowed('https://example.com', config)).toBe(true);
    expect(isOriginValueAllowed('http://example.com', config)).toBe(true);
  });

  it('rejeita subdomínio quando não há wildcard', () => {
    expect(isOriginValueAllowed('https://sub.example.com', config)).toBe(false);
  });

  it('rejeita domínio diferente', () => {
    expect(isOriginValueAllowed('https://evil.com', config)).toBe(false);
  });

  it('rejeita quando a allowlist está vazia', () => {
    expect(isOriginValueAllowed('https://example.com', {})).toBe(false);
  });

  it('rejeita origem não parseável', () => {
    expect(isOriginValueAllowed('not a url', config)).toBe(false);
  });

  it('respeita o protocolo quando o domínio o especifica', () => {
    const httpsOnly = { allowedDomains: ['https://example.com'] };
    expect(isOriginValueAllowed('https://example.com', httpsOnly)).toBe(true);
    expect(isOriginValueAllowed('http://example.com', httpsOnly)).toBe(false);
  });
});

describe('isOriginValueAllowed — wildcard', () => {
  const config = { allowedDomains: ['*.example.com'] };

  it('aceita subdomínio direto', () => {
    expect(isOriginValueAllowed('https://app.example.com', config)).toBe(true);
  });

  it('aceita subdomínio aninhado', () => {
    expect(isOriginValueAllowed('https://a.b.example.com', config)).toBe(true);
  });

  it('aceita o apex domain (igual ao hostname do wildcard)', () => {
    expect(isOriginValueAllowed('https://example.com', config)).toBe(true);
  });

  it('rejeita domínio que apenas contém o sufixo sem ser subdomínio', () => {
    expect(isOriginValueAllowed('https://notexample.com', config)).toBe(false);
    expect(isOriginValueAllowed('https://example.com.evil.com', config)).toBe(false);
  });
});

describe('isOriginValueAllowed — porta', () => {
  it('domínio com porta exige host (hostname:porta) idêntico', () => {
    const config = { allowedDomains: ['localhost:3000'] };
    expect(isOriginValueAllowed('http://localhost:3000', config)).toBe(true);
    expect(isOriginValueAllowed('http://localhost:4000', config)).toBe(false);
    expect(isOriginValueAllowed('http://localhost', config)).toBe(false);
  });

  it('domínio sem porta casa por hostname ignorando a porta da origem', () => {
    const config = { allowedDomains: ['localhost'] };
    expect(isOriginValueAllowed('http://localhost:3000', config)).toBe(true);
    expect(isOriginValueAllowed('http://localhost', config)).toBe(true);
  });
});

describe('isOriginValueAllowed — IDN (internationalized domain names)', () => {
  // O construtor URL normaliza o hostname IDN para punycode (ASCII). A allowlist
  // só casa quando contém a forma punycode — a entrada unicode literal não é
  // convertida por parseAllowedDomain. Este teste caracteriza esse comportamento.
  const idnOrigin = 'https://münchen.de';

  it('normaliza o hostname IDN da origem para punycode', () => {
    expect(parseOrigin(idnOrigin)?.hostname).toBe('xn--mnchen-3ya.de');
  });

  it('aceita quando a allowlist contém a forma punycode', () => {
    expect(isOriginValueAllowed(idnOrigin, { allowedDomains: ['xn--mnchen-3ya.de'] })).toBe(true);
  });

  it('aceita IDN via wildcard punycode', () => {
    expect(
      isOriginValueAllowed('https://shop.münchen.de', { allowedDomains: ['*.xn--mnchen-3ya.de'] }),
    ).toBe(true);
  });

  it('não casa a forma unicode literal na allowlist', () => {
    expect(isOriginValueAllowed(idnOrigin, { allowedDomains: ['münchen.de'] })).toBe(false);
  });
});

describe('parseAllowedDomain', () => {
  it('ignora entradas vazias e o coringa total "*"', () => {
    expect(parseAllowedDomain('')).toBeNull();
    expect(parseAllowedDomain('   ')).toBeNull();
    expect(parseAllowedDomain('*')).toBeNull();
  });

  it('extrai protocolo, host e detecta wildcard/porta', () => {
    expect(parseAllowedDomain('https://*.example.com')).toEqual({
      protocol: 'https:',
      host: 'example.com',
      hostname: 'example.com',
      wildcard: true,
      hasPort: false,
    });

    expect(parseAllowedDomain('localhost:3000')).toEqual({
      protocol: '',
      host: 'localhost:3000',
      hostname: 'localhost',
      wildcard: false,
      hasPort: true,
    });
  });
});
