"""
Tavily Service - Busca na Web otimizada para LLMs.
"""

import logging

import requests
from tavily import TavilyClient
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..core.config import settings

logger = logging.getLogger(__name__)

# Teto de tempo POR TENTATIVA passado ao requests (connect+read). Sem isto, a busca
# herda o timeout interno de 60s do TavilyClient e, com retry, o pior caso estoura o
# TTFT (meta p95 ≤ 15s). Com 2 tentativas: 7s + ~0.4s de backoff + 7s ≈ 14.4s no pior
# caso (duas falhas lentas seguidas — search já condenada). O caso comum termina em <3s.
_TAVILY_TIMEOUT_SECONDS = 7

# Quedas TRANSIENTES de CONEXÃO (não de timeout). Bug alvo: o keepalive do
# requests.Session interno do Tavily morre em idle e o próximo POST estoura
# "RemoteDisconnected", que o SDK propaga COMO requests.exceptions.ConnectionError — o
# SDK só intercepta requests.exceptions.Timeout e o re-levanta como TimeoutError builtin
# (tavily.py:153), logo TIMEOUTS não entram aqui nem são retentados (preserva o TTFT).
# Sutileza: requests.ConnectTimeout É subclasse de ConnectionError e casaria neste tuple,
# MAS o SDK o intercepta como Timeout antes de chegar ao retry — por isso timeout real
# nunca é retentado. ChunkedEncodingError (corpo do POST cortado no meio) também é
# transiente; ficamos em 2 tentativas (1 retry só) para, no caso raro de o servidor já
# ter contabilizado a busca antes do corte, gerar no MÁXIMO 1 cobrança extra na Tavily.
_TAVILY_TRANSIENT = (
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
)

_tavily_retry = retry(
    stop=stop_after_attempt(2),  # 1 retry — a queda de keepalive falha na hora; basta reabrir a conexão
    wait=wait_exponential(multiplier=0.4, min=0.4, max=2),  # 1 espera de ~0.4s entre as 2 tentativas
    retry=retry_if_exception_type(_TAVILY_TRANSIENT),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)


class TavilyService:
    """
    Serviço de Busca na Web usando Tavily AI (Otimizado para LLMs).
    """

    def __init__(self):
        self.api_key = settings.TAVILY_API_KEY
        self.client = None

        if self.api_key:
            try:
                self.client = TavilyClient(api_key=self.api_key)
                logger.info("✅ TavilyService inicializado com sucesso")
            except Exception as e:
                logger.error(f"❌ Erro ao inicializar Tavily: {e}")
        else:
            logger.warning("⚠️ TAVILY_API_KEY não configurada. Web search desativado.")

    @_tavily_retry
    def _raw_search(self, query: str, max_results: int) -> dict:
        """Chamada HTTP crua ao Tavily (POST), com retry SÓ em queda transiente de
        conexão (keepalive morto → RemoteDisconnected). Fica FORA do ``try/except`` amplo
        de ``search`` de propósito: a tenacity precisa ver a exceção antes do catch
        genérico engoli-la. ``timeout`` põe teto por tentativa (connect+read) para caber
        no TTFT em vez de herdar os 60s internos do SDK. ``search_depth='basic'`` p/
        latência otimizada."""
        return self.client.search(
            query=query,
            search_depth="basic",  # Configuração aprovada
            max_results=max_results,
            timeout=_TAVILY_TIMEOUT_SECONDS,
        )

    def search(self, query: str, max_results: int = 3) -> str:
        """
        Executa busca na web e retorna contexto formatado para o LLM.

        Args:
            query: Pergunta ou termo de busca
            max_results: Número máximo de resultados (padrão: 3)

        Returns:
            String formatada com resultados ou mensagem de erro
        """
        if not self.client:
            return "❌ Erro: A busca na web não está configurada no sistema (TAVILY_API_KEY ausente)."

        try:
            logger.info(f"[WebSearch] Buscando: '{query}'")

            response = self._raw_search(query, max_results)

            results = response.get("results", [])

            if not results:
                logger.warning(f"[WebSearch] Nenhum resultado para: '{query}'")
                return (
                    "ℹ️ Nenhum resultado relevante encontrado na web para essa consulta."
                )

            # Formata para leitura fácil do LLM
            formatted = []
            for idx, res in enumerate(results, 1):
                title = res.get("title", "Sem título")
                content = res.get("content", "")
                url = res.get("url", "")

                formatted.append(
                    f"🌐 **Resultado {idx}:** [{title}]({url})\n"
                    f"**Conteúdo:** {content}\n"
                )

            final_output = "\n---\n\n".join(formatted)
            logger.info(f"[WebSearch] Retornou {len(results)} resultados")

            return final_output

        except Exception as e:
            logger.error(f"[WebSearch] Erro na busca: {e}", exc_info=True)
            return f"❌ Erro ao realizar busca na web: {str(e)}"


# Singleton
_tavily_service = None


def get_tavily_service():
    """Retorna instância singleton do TavilyService."""
    global _tavily_service
    if _tavily_service is None:
        _tavily_service = TavilyService()
    return _tavily_service
