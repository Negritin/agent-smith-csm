/**
 * MEDIO-007 — Paridade SSRF (lado TypeScript).
 *
 * Os dois validadores de URL externa implementam a MESMA política SSRF em
 * runtimes distintos (code-share impossível):
 *   - TS:     lib/security/url-validator.ts          (CIDRs manuais)
 *   - Python: backend/app/core/security/url_validator.py (módulo ipaddress)
 *
 * Este teste consome o FIXTURE CANÔNICO ÚNICO (test-fixtures/ssrf-parity-cases.json)
 * — o mesmo arquivo lido pelo teste irmão em Python
 * (backend/tests/security/test_ssrf_parity.py). Para cada IP do fixture, o
 * veredito de `validateExternalUrl` deve bater com o `blocked` esperado. Se uma
 * das duas implementações divergir no futuro, o respectivo lado quebra o CI
 * (.github/workflows/ssrf-parity.yml).
 */
import { describe, expect, it } from 'vitest';

import cases from '../../test-fixtures/ssrf-parity-cases.json';
import { ExternalUrlValidationError, validateExternalUrl } from '@/lib/security/url-validator';

interface ParityCase {
  ip: string;
  blocked: boolean;
  note: string;
}

const parityCases: ParityCase[] = cases.cases;

/** IPv6 literais precisam de colchetes para virar uma URL válida. */
function toUrl(ip: string): string {
  const host = ip.includes(':') ? `[${ip}]` : ip;
  return `https://${host}/`;
}

describe('SSRF parity (TS) — fixture canônico compartilhado com o Python', () => {
  it('o fixture não está vazio', () => {
    expect(parityCases.length).toBeGreaterThan(0);
  });

  for (const testCase of parityCases) {
    const verdict = testCase.blocked ? 'bloqueia' : 'permite';
    it(`${verdict} ${testCase.ip} (${testCase.note})`, async () => {
      const url = toUrl(testCase.ip);

      if (testCase.blocked) {
        await expect(validateExternalUrl(url)).rejects.toBeInstanceOf(ExternalUrlValidationError);
      } else {
        const result = await validateExternalUrl(url);
        expect(result.url.protocol).toBe('https:');
        expect(result.resolvedAddresses.length).toBeGreaterThan(0);
      }
    });
  }
});
