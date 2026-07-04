"""External URL validation for SSRF-sensitive integrations.

PARIDADE SSRF (MEDIO-007): esta política DEVE permanecer equivalente à versão
TypeScript em ``lib/security/url-validator.ts``. Os runtimes são distintos (módulo
``ipaddress`` aqui; CIDRs manuais lá), então não há code-share: a paridade é
travada por um FIXTURE CANÔNICO ÚNICO (``test-fixtures/ssrf-parity-cases.json``)
consumido pelos testes dos dois lados:
  - Python: backend/tests/security/test_ssrf_parity.py
  - TS:     lib/security/url-validator.parity.test.ts
Ambos rodam no CI (``.github/workflows/ssrf-parity.yml``). Ao alterar QUALQUER
faixa bloqueada/permitida aqui, atualize o validador TS e o fixture juntos —
divergência quebra o CI.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import socket
from urllib.parse import urlparse


class ExternalUrlValidationError(ValueError):
    """Raised when a URL is not safe for outbound server-side requests."""


@dataclass(frozen=True)
class ValidatedExternalUrl:
    original_url: str
    normalized_url: str
    hostname: str
    resolved_addresses: tuple[str, ...]


def _normalize_hostname(hostname: str | None) -> str:
    if not hostname:
        return ""
    return hostname.strip("[]").rstrip(".").lower()


def _is_blocked_hostname(hostname: str) -> bool:
    return (
        hostname == "localhost"
        or hostname.endswith(".localhost")
        or hostname in {"ip6-localhost", "ip6-loopback"}
    )


def _is_blocked_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError as exc:
        raise ExternalUrlValidationError("URL resolves to an invalid IP address") from exc

    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return True

    return (
        not ip.is_global
        or ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_hostname(hostname: str) -> tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ExternalUrlValidationError("Unable to resolve URL hostname") from exc

    addresses: list[str] = []
    seen: set[str] = set()
    for info in infos:
        address = info[4][0]
        if address not in seen:
            seen.add(address)
            addresses.append(address)

    if not addresses:
        raise ExternalUrlValidationError("URL hostname resolved no addresses")

    return tuple(addresses)


def validate_external_url(raw_url: str) -> ValidatedExternalUrl:
    """Validate that an outbound URL is HTTPS and resolves only to public IPs."""
    parsed = urlparse(raw_url)

    if parsed.scheme.lower() != "https":
        raise ExternalUrlValidationError("Only HTTPS URLs are allowed")

    if parsed.username or parsed.password:
        raise ExternalUrlValidationError("URL credentials are not allowed")

    hostname = _normalize_hostname(parsed.hostname)
    if not hostname or _is_blocked_hostname(hostname):
        raise ExternalUrlValidationError("Blocked hostname")

    try:
        ipaddress.ip_address(hostname)
        resolved_addresses = (hostname,)
    except ValueError:
        resolved_addresses = _resolve_hostname(hostname)

    for address in resolved_addresses:
        if _is_blocked_ip(address):
            raise ExternalUrlValidationError("URL resolves to a blocked IP address")

    return ValidatedExternalUrl(
        original_url=raw_url,
        normalized_url=parsed.geturl(),
        hostname=hostname,
        resolved_addresses=resolved_addresses,
    )


def revalidate_external_url(validated: ValidatedExternalUrl) -> ValidatedExternalUrl:
    """Re-resolve a validated URL immediately before request execution."""
    latest = validate_external_url(validated.original_url)

    if latest.hostname != validated.hostname:
        raise ExternalUrlValidationError("URL hostname changed before request")

    if set(latest.resolved_addresses) != set(validated.resolved_addresses):
        raise ExternalUrlValidationError("URL DNS resolution changed before request")

    return latest
