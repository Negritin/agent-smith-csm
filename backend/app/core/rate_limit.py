"""
Rate Limiting Configuration
Usando slowapi para proteger endpoints críticos contra abuso.
"""

import logging

from slowapi import Limiter
from starlette.requests import Request

from app.core.config import settings
from app.core.redis import get_async_redis_client

logger = logging.getLogger(__name__)


def get_real_client_ip(request: Request) -> str:
    """
    Extrai o IP real do cliente para uso como chave do rate limiter.

    Em produção (Railway/Vercel) o request passa por reverse proxy(es) que
    APPENDA o IP observado ao X-Forwarded-For:

        X-Forwarded-For: <cliente>, <proxy1>, ..., <proxyN_mais_proximo_do_app>

    O valor injetado pelo proxy MAIS PRÓXIMO do app é XFF[-1]; com N hops
    confiáveis (settings.TRUSTED_PROXY_HOPS), o IP do cliente real é XFF[-N].

    SEGURANÇA (F06): NÃO usamos XFF[0] — esse é o primeiro valor da lista e é
    totalmente controlado pelo cliente, então um atacante o rotaciona a cada
    request e cai sempre num bucket novo, anulando o limite. Pegando XFF[-N]
    contamos a partir do proxy confiável.

    Como o ProxyHeadersMiddleware NÃO remove o header XFF cru, um atacante que
    alcança a origem diretamente (ou injeta hops falsos) ainda controla a cauda
    da lista. Por isso, se a lista tiver MENOS que N entradas (curta ou forjada
    sem os hops reais), caímos no fallback `request.client.host` em vez de
    confiar num token controlado pelo atacante.
    """
    hops = settings.TRUSTED_PROXY_HOPS
    forwarded = request.headers.get("X-Forwarded-For")

    if forwarded and hops >= 1:
        parts = [ip.strip() for ip in forwarded.split(",") if ip.strip()]
        # Só confiamos no hop do proxy se a cadeia tiver pelo menos N entradas.
        # Caso contrário a cauda é controlada pelo cliente → fallback seguro.
        if len(parts) >= hops:
            candidate = parts[-hops]
            if candidate:
                return candidate

    client = request.client.host if request.client else None
    return client or "127.0.0.1"


# Limiter global - usa IP real do cliente como chave de identificação
limiter = Limiter(key_func=get_real_client_ip)


# ===== Limiter manual de auth-falha de webhook (anti-enumeração) =====
#
# O @limiter do slowapi roda no key_func ANTES do corpo/lookup e serve como
# bound grosso por IP (120/minute). Aqui implementamos um contador Redis
# explícito de FALHAS de autenticação por IP, escopado ao prefixo wh_, para
# travar enumeração de tokens (cada tentativa com token inválido conta; um
# atacante que varre o espaço de tokens estoura o limite e leva 429).
#
# Janela curta com limite baixo: tráfego legítimo de webhook nunca falha auth
# de forma repetida (o token é fixo e válido), então só enumeração acumula.

# Limite de falhas de auth por IP dentro da janela antes de retornar 429.
WEBHOOK_AUTH_FAIL_LIMIT = 20
# Janela (segundos) do contador de falhas de auth por IP.
WEBHOOK_AUTH_FAIL_WINDOW = 60


async def record_webhook_auth_failure(request: Request, *, prefix: str = "wh_") -> bool:
    """
    Registra UMA falha de autenticação de webhook para o IP do request e diz
    se o limite foi estourado (→ o resolver deve responder 429).

    Mecânica: INCR de um contador Redis namespaceado por IP; no PRIMEIRO hit da
    janela (valor == 1) seta EXPIRE para WEBHOOK_AUTH_FAIL_WINDOW. Retorna True
    quando o contador ULTRAPASSA WEBHOOK_AUTH_FAIL_LIMIT dentro da janela.

    `prefix` (default 'wh_') namespaceia o bucket para o domínio de tokens de
    webhook — mantém o contador isolado de quaisquer outros limites por IP.

    Fail-open: se o Redis estiver indisponível, NÃO bloqueia (retorna False) e
    apenas loga. O @limiter por IP (120/minute) e o 401 genérico do lookup
    permanecem como defesa; este contador é uma camada adicional anti-enumeração,
    não a fronteira de auth.
    """
    ip = get_real_client_ip(request)
    key = f"webhook_auth_fail:{prefix}:{ip}"

    try:
        redis = await get_async_redis_client()
        count = await redis.incr(key)
        # Só seta o TTL no primeiro hit da janela para não reiniciar o relógio
        # a cada falha (senão a janela nunca expiraria sob ataque contínuo).
        if count == 1:
            await redis.expire(key, WEBHOOK_AUTH_FAIL_WINDOW)
        return count > WEBHOOK_AUTH_FAIL_LIMIT
    except Exception as e:
        logger.warning(
            "[RATE_LIMIT] Falha ao registrar auth-falha de webhook "
            "(fail-open, não bloqueia): %s",
            e,
        )
        return False


# Limite de mensagens por integração dentro da janela (bound por tenant no
# caminho de SUCESSO — evita que um único token válido inunde a borda).
WEBHOOK_INTEGRATION_LIMIT = 600
# Janela (segundos) do contador por integração.
WEBHOOK_INTEGRATION_WINDOW = 60


async def record_webhook_integration_hit(integration_id: str) -> bool:
    """
    Registra UM acesso bem-sucedido (token válido resolvido) para a integração
    e diz se o limite por tenant foi estourado (→ o resolver pode responder 429).

    Mesma mecânica INCR+EXPIRE do contador de falhas, mas chaveado por
    integration_id (não por IP), aplicando um teto de vazão por tenant no
    caminho de sucesso.

    Fail-open: indisponibilidade do Redis não bloqueia (retorna False).
    """
    key = f"webhook_integration_hit:{integration_id}"

    try:
        redis = await get_async_redis_client()
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, WEBHOOK_INTEGRATION_WINDOW)
        return count > WEBHOOK_INTEGRATION_LIMIT
    except Exception as e:
        logger.warning(
            "[RATE_LIMIT] Falha ao registrar hit por integração "
            "(fail-open, não bloqueia): %s",
            e,
        )
        return False
