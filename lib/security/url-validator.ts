/**
 * Validação de URL externa (anti-SSRF) — implementação TypeScript.
 *
 * PARIDADE SSRF (MEDIO-007): esta política DEVE permanecer equivalente à versão
 * Python em backend/app/core/security/url_validator.py. Os runtimes são distintos
 * (CIDRs manuais aqui; módulo `ipaddress` lá), então não há code-share: a paridade
 * é travada por um FIXTURE CANÔNICO ÚNICO (test-fixtures/ssrf-parity-cases.json)
 * consumido pelos testes dos dois lados:
 *   - TS:     lib/security/url-validator.parity.test.ts
 *   - Python: backend/tests/security/test_ssrf_parity.py
 * Ambos rodam no CI (.github/workflows/ssrf-parity.yml). Ao alterar QUALQUER faixa
 * bloqueada/permitida aqui, atualize o validador Python e o fixture juntos —
 * divergência quebra o CI.
 */
import { promises as dns } from 'node:dns';
import type { LookupAddress } from 'node:dns';
import net from 'node:net';

export const MAX_EXTERNAL_RESPONSE_BYTES = 10 * 1024 * 1024;

export class ExternalUrlValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ExternalUrlValidationError';
  }
}

export interface ValidatedExternalUrl {
  url: URL;
  hostname: string;
  resolvedAddresses: string[];
}

function normalizeHostname(hostname: string): string {
  return hostname.replace(/^\[/, '').replace(/\]$/, '').replace(/\.$/, '').toLowerCase();
}

function parseIpv4ToInt(address: string): number | null {
  const parts = address.split('.');
  if (parts.length !== 4) return null;

  let value = 0;
  for (const part of parts) {
    if (!/^\d+$/.test(part)) return null;
    const octet = Number(part);
    if (octet < 0 || octet > 255) return null;
    value = (value << 8) + octet;
  }

  return value >>> 0;
}

function isIpv4InCidr(address: string, base: string, prefixLength: number): boolean {
  const addressInt = parseIpv4ToInt(address);
  const baseInt = parseIpv4ToInt(base);
  if (addressInt === null || baseInt === null) return false;

  const mask = prefixLength === 0 ? 0 : (0xffffffff << (32 - prefixLength)) >>> 0;
  return (addressInt & mask) === (baseInt & mask);
}

function isBlockedIPv4(address: string): boolean {
  const blockedRanges: Array<[string, number]> = [
    ['0.0.0.0', 8],
    ['10.0.0.0', 8],
    ['100.64.0.0', 10],
    ['127.0.0.0', 8],
    ['169.254.0.0', 16],
    ['172.16.0.0', 12],
    ['192.0.0.0', 24],
    ['192.0.2.0', 24],
    ['192.168.0.0', 16],
    ['198.18.0.0', 15],
    ['198.51.100.0', 24],
    ['203.0.113.0', 24],
    ['224.0.0.0', 4],
    ['240.0.0.0', 4],
  ];

  return blockedRanges.some(([base, prefix]) => isIpv4InCidr(address, base, prefix));
}

function ipv4ToHextets(address: string): [string, string] | null {
  const value = parseIpv4ToInt(address);
  if (value === null) return null;

  const high = ((value >>> 16) & 0xffff).toString(16);
  const low = (value & 0xffff).toString(16);
  return [high, low];
}

function parseIPv6ToBigInt(input: string): bigint | null {
  let address = input.toLowerCase();
  if (address.includes('%')) {
    address = address.split('%')[0];
  }

  const lastColon = address.lastIndexOf(':');
  const ipv4Tail = lastColon >= 0 ? address.slice(lastColon + 1) : '';
  if (net.isIP(ipv4Tail) === 4) {
    const hextets = ipv4ToHextets(ipv4Tail);
    if (!hextets) return null;
    address = `${address.slice(0, lastColon)}:${hextets[0]}:${hextets[1]}`;
  }

  const compressedParts = address.split('::');
  if (compressedParts.length > 2) return null;

  const head = compressedParts[0] ? compressedParts[0].split(':') : [];
  const tail = compressedParts.length === 2 && compressedParts[1] ? compressedParts[1].split(':') : [];
  const missing = 8 - head.length - tail.length;

  if (compressedParts.length === 1 && missing !== 0) return null;
  if (compressedParts.length === 2 && missing < 1) return null;

  const parts = [...head, ...Array(Math.max(missing, 0)).fill('0'), ...tail];
  if (parts.length !== 8) return null;

  let value = BigInt(0);
  for (const part of parts) {
    if (!/^[0-9a-f]{1,4}$/.test(part)) return null;
    value = (value << BigInt(16)) + BigInt(parseInt(part, 16));
  }

  return value;
}

function isIPv6InCidr(address: string, base: string, prefixLength: number): boolean {
  const addressInt = parseIPv6ToBigInt(address);
  const baseInt = parseIPv6ToBigInt(base);
  if (addressInt === null || baseInt === null) return false;

  if (prefixLength === 0) return true;
  const mask = ((BigInt(1) << BigInt(prefixLength)) - BigInt(1)) << BigInt(128 - prefixLength);
  return (addressInt & mask) === (baseInt & mask);
}

function isBlockedIPv6(address: string): boolean {
  const blockedRanges: Array<[string, number]> = [
    ['::', 128],
    ['::1', 128],
    ['::ffff:0:0', 96],
    ['64:ff9b::', 96],
    ['100::', 64],
    ['2001::', 32],
    ['2001:db8::', 32],
    ['2002::', 16],
    ['fc00::', 7],
    ['fe80::', 10],
    ['ff00::', 8],
  ];

  return blockedRanges.some(([base, prefix]) => isIPv6InCidr(address, base, prefix));
}

function isBlockedHostname(hostname: string): boolean {
  return (
    hostname === 'localhost' ||
    hostname.endsWith('.localhost') ||
    hostname === 'ip6-localhost' ||
    hostname === 'ip6-loopback'
  );
}

function assertPublicAddress(address: string): void {
  const version = net.isIP(address);

  if (version === 4 && isBlockedIPv4(address)) {
    throw new ExternalUrlValidationError('URL resolves to a blocked IPv4 address');
  }

  if (version === 6 && isBlockedIPv6(normalizeHostname(address))) {
    throw new ExternalUrlValidationError('URL resolves to a blocked IPv6 address');
  }

  if (version === 0) {
    throw new ExternalUrlValidationError('URL resolves to an invalid IP address');
  }
}

function assertSameResolvedAddresses(expected: string[], actual: string[]): void {
  const expectedSet = new Set(expected);
  const actualSet = new Set(actual);

  if (expectedSet.size !== actualSet.size) {
    throw new ExternalUrlValidationError('URL DNS resolution changed before request');
  }

  for (const address of Array.from(expectedSet)) {
    if (!actualSet.has(address)) {
      throw new ExternalUrlValidationError('URL DNS resolution changed before request');
    }
  }
}

export async function validateExternalUrl(rawUrl: string): Promise<ValidatedExternalUrl> {
  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
  } catch {
    throw new ExternalUrlValidationError('Invalid URL');
  }

  if (parsed.protocol !== 'https:') {
    throw new ExternalUrlValidationError('Only HTTPS URLs are allowed');
  }

  if (parsed.username || parsed.password) {
    throw new ExternalUrlValidationError('URL credentials are not allowed');
  }

  const hostname = normalizeHostname(parsed.hostname);
  if (!hostname || isBlockedHostname(hostname)) {
    throw new ExternalUrlValidationError('Blocked hostname');
  }

  const literalVersion = net.isIP(hostname);
  if (literalVersion !== 0) {
    assertPublicAddress(hostname);
    return { url: parsed, hostname, resolvedAddresses: [hostname] };
  }

  let records: LookupAddress[];
  try {
    records = await dns.lookup(hostname, { all: true, verbatim: false });
  } catch {
    throw new ExternalUrlValidationError('Unable to resolve URL hostname');
  }

  const resolvedAddresses = Array.from(new Set(records.map((record) => record.address)));
  if (resolvedAddresses.length === 0) {
    throw new ExternalUrlValidationError('URL hostname resolved no addresses');
  }

  for (const address of resolvedAddresses) {
    assertPublicAddress(address);
  }

  return { url: parsed, hostname, resolvedAddresses };
}

export async function revalidateExternalUrl(
  validated: ValidatedExternalUrl,
): Promise<ValidatedExternalUrl> {
  const latest = await validateExternalUrl(validated.url.toString());

  if (latest.hostname !== validated.hostname) {
    throw new ExternalUrlValidationError('URL hostname changed before request');
  }

  assertSameResolvedAddresses(validated.resolvedAddresses, latest.resolvedAddresses);
  return latest;
}
