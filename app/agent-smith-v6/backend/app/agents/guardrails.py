import asyncio
import concurrent.futures
import logging
import os
import re
from typing import Any, Dict, List, Tuple

from app.services.llama_guard_service import get_llama_guard_service
from app.services.presidio_service import get_presidio_service

logger = logging.getLogger(__name__)

# Timeout para regex (evita ReDoS)
REGEX_TIMEOUT_SECONDS = 1.0

# Teto de caracteres submetidos ao Presidio (NER spaCy é CPU-bound). Trunca
# entradas/saídas patológicas ANTES do parse para fechar um vetor de DoS de CPU
# (F22). PII real (CPF/e-mail/telefone) aparece bem antes deste limite numa
# mensagem de chat; o excedente não é analisado.
PRESIDIO_MAX_INPUT_CHARS = 10000

# Singleton — evita criar/destruir executor a cada chamada de regex
_REGEX_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=min(8, (os.cpu_count() or 4) + 2),
    thread_name_prefix="guardrail-regex"
)

# =============================================================================
# PROMPT INJECTION PATTERNS (PT-BR + EN)
# Detecta tentativas conhecidas de jailbreak e prompt injection
# =============================================================================
PROMPT_INJECTION_PATTERNS: List[str] = [
    # =========================================================================
    # SYSTEM MESSAGE OVERRIDE (Fake system messages)
    # =========================================================================
    r"#\s*SYSTEM\s*(MESSAGE)?",
    r"\[SYSTEM\]",
    r"<\s*system\s*>",
    r"SYSTEM\s*PROMPT\s*:",
    r"<<\s*SYS\s*>>",
    r"\[INST\]",

    # =========================================================================
    # IGNORE INSTRUCTIONS (EN)
    # =========================================================================
    r"(?i)ignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?|directions?)",
    r"(?i)disregard\s+(all\s+)?(previous|prior|above|earlier)",
    r"(?i)forget\s+(everything|all|your)\s+(instructions?|rules?|training|programming)",
    r"(?i)override\s+(your\s+)?(previous|prior|all)\s+(instructions?|rules?)",
    r"(?i)do\s+not\s+follow\s+(your\s+)?(previous|prior|original)",

    # =========================================================================
    # IGNORE INSTRUCTIONS (PT-BR)
    # =========================================================================
    r"(?i)ignore\s+(todas?\s+)?(as\s+)?(instru[çc][õo]es|regras|orienta[çc][õo]es)",
    r"(?i)ignor[ea]\s+(todas?\s+)?(as\s+)?(instru[çc][õo]es|regras)",
    r"(?i)desconsider[ea]\s+(todas?\s+)?(as\s+)?(instru[çc][õo]es|regras)",
    r"(?i)esque[çc]a\s+(todas?\s+)?(as\s+)?(instru[çc][õo]es|regras|tudo)",
    r"(?i)n[ãa]o\s+siga\s+(as\s+)?(instru[çc][õo]es|regras)",
    r"(?i)abandone\s+(suas?\s+)?(instru[çc][õo]es|regras|programa[çc][ãa]o)",

    # =========================================================================
    # ROLE PLAY ATTACKS (EN)
    # =========================================================================
    r"(?i)you\s+are\s+now\s+(a|an)\s+",
    r"(?i)pretend\s+(you\s+are|to\s+be)\s+",
    r"(?i)act\s+as\s+(if\s+you\s+were|a|an)\s+",
    r"(?i)roleplay\s+as\s+",
    r"(?i)simulate\s+(being|a|an)\s+",
    r"(?i)from\s+now\s+on\s+you\s+(are|will\s+be)",

    # =========================================================================
    # ROLE PLAY ATTACKS (PT-BR)
    # =========================================================================
    r"(?i)voc[êe]\s+agora\s+[ée]\s+(um|uma)",
    r"(?i)finja\s+(que\s+)?(voc[êe]\s+)?[ée]\s+",
    r"(?i)finja\s+ser\s+",
    r"(?i)atue\s+como\s+(se\s+)?(fosse\s+)?(um|uma)?",
    r"(?i)simule\s+(ser\s+)?(um|uma)?",
    r"(?i)interprete\s+(o\s+papel\s+de\s+)?",
    r"(?i)comporte-se\s+como\s+",
    r"(?i)a\s+partir\s+de\s+agora\s+voc[êe]\s+[ée]",

    # =========================================================================
    # JAILBREAK PHRASES (EN)
    # =========================================================================
    r"(?i)\bDAN\b\s*(mode)?",
    r"(?i)Developer\s+Mode",
    r"(?i)\bjailbreak\b",
    r"(?i)bypass\s+(your\s+)?(restrictions?|filters?|safety|rules?)",
    r"(?i)unlock\s+(your\s+)?(full|true)\s+(potential|capabilities)",
    r"(?i)remove\s+(your\s+)?(limitations?|restrictions?|filters?)",
    r"(?i)disable\s+(your\s+)?(safety|filters?|restrictions?)",
    r"(?i)evil\s*(mode|version)",
    r"(?i)uncensored\s*(mode)?",
    r"(?i)unrestricted\s*(mode)?",
    r"(?i)no\s*(rules?|limits?|restrictions?)\s*(mode)?",

    # =========================================================================
    # JAILBREAK PHRASES (PT-BR)
    # =========================================================================
    r"(?i)modo\s+(desenvolvedor|dev|programador)",
    r"(?i)modo\s+(sem\s+)?(restri[çc][õo]es|limites|filtros|censura)",
    r"(?i)desativ[ea]\s+(suas?\s+)?(restri[çc][õo]es|filtros|seguran[çc]a)",
    r"(?i)remov[ea]\s+(suas?\s+)?(restri[çc][õo]es|filtros|limita[çc][õo]es)",
    r"(?i)liber[ea]\s+(suas?\s+)?(restri[çc][õo]es|capacidades)",
    r"(?i)sem\s+(restri[çc][õo]es|filtros|limites|censura)",
    r"(?i)vers[ãa]o\s+(sem\s+filtro|desbloqueada|completa)",

    # =========================================================================
    # OUTPUT MANIPULATION / PROMPT EXTRACTION (EN)
    # =========================================================================
    r"(?i)reveal\s+(your\s+)?(system\s+)?prompt",
    r"(?i)show\s+(me\s+)?(your\s+)?(system\s+)?(instructions?|prompt)",
    r"(?i)what\s+(is|are)\s+your\s+(system\s+)?prompt",
    r"(?i)print\s+(your\s+)?(initial|system|original)\s+(prompt|instructions?)",
    r"(?i)output\s+(your\s+)?(system\s+)?prompt",
    r"(?i)display\s+(your\s+)?(hidden|secret|system)\s+(instructions?|prompt)",
    r"(?i)tell\s+me\s+(your\s+)?(system\s+)?(prompt|instructions?)",
    r"(?i)repeat\s+(your\s+)?(system\s+)?(prompt|instructions?)",

    # =========================================================================
    # OUTPUT MANIPULATION / PROMPT EXTRACTION (PT-BR)
    # =========================================================================
    r"(?i)revel[ea]\s+(seu\s+)?prompt",
    r"(?i)mostr[ea]\s+(seu\s+|suas?\s+)?(prompt|instru[çc][õo]es)",
    r"(?i)qual\s+[ée]\s+(seu\s+|o\s+seu\s+)?prompt",
    r"(?i)diga\s+(seu\s+|suas?\s+)?(prompt|instru[çc][õo]es)",
    r"(?i)exib[ae]\s+(seu\s+|suas?\s+)?(prompt|instru[çc][õo]es)",
    r"(?i)imprima\s+(seu\s+)?prompt",
    r"(?i)como\s+(voc[êe]\s+)?(est[áa]|foi)\s+configurado",
    r"(?i)como\s+voc[êe]\s+foi\s+programado",
    r"(?i)quais\s+s[ãa]o\s+(suas?\s+)?instru[çc][õo]es",
    r"(?i)me\s+conte\s+(suas?\s+)?instru[çc][õo]es",

    # =========================================================================
    # DEVELOPER/DEBUG COMMANDS
    # =========================================================================
    r"(?i)/debug",
    r"(?i)/admin",
    r"(?i)/sudo",
    r"(?i)/root",
    r"(?i)/override",
    r"(?i)/bypass",
    r"(?i)\[DEBUG\]",
    r"(?i)\[ADMIN\]",
    r"(?i)```system",
    r"(?i)```instruction",
]

# Pre-compiled injection patterns (compiled once at import time)
_COMPILED_INJECTION_PATTERNS = [re.compile(p) for p in PROMPT_INJECTION_PATTERNS]

# =============================================================================
# TLDs CONHECIDOS (Para validação de URLs sem protocolo)
# =============================================================================
KNOWN_TLDS = (
    # Genéricos populares
    "com", "net", "org", "info", "biz", "name", "pro",
    # Tech
    "io", "co", "ai", "app", "dev", "tech", "cloud", "digital",
    # Encurtadores comuns
    "ly", "me", "to", "cc", "gl", "gd", "gg", "link", "click",
    # Países principais
    "br", "us", "uk", "de", "fr", "es", "it", "pt", "ru", "cn", "jp", "in", "au", "ca", "mx",
    # Brasil específicos
    "com.br", "org.br", "net.br", "gov.br", "edu.br",
    # Outros
    "edu", "gov", "mil", "xyz", "online", "site", "website", "store", "shop",
)

# Regex pattern para TLDs (gerado da tupla)
_TLD_PATTERN = r"(?:" + "|".join(re.escape(tld) for tld in KNOWN_TLDS) + r")"

# =============================================================================
# DEFAULT BLOCKLIST (Encurtadores e Maliciosos conhecidos)
# =============================================================================
DEFAULT_BLACKLIST: List[str] = [
    # Encurtadores (muito usados para phishing)
    "bit.ly",
    "tinyurl.com",
    "t.co",
    "goo.gl",
    "shorturl.at",
    "rb.gy",
    "is.gd",
    "owl.li",
    "shorte.st",
    "adf.ly",
    "bc.vc",
    "snip.ly",
    "po.st",
    "q.gs",

    # Domínios maliciosos conhecidos (exemplo)
    "malware.com",
    "phishing.org"
]
TOXIC_BLOCK_PATTERNS: List[str] = [
    # Ameaças
    r"(?i)vou\s+te\s+(matar|acabar|destruir)",
    r"(?i)morre(r)?\s+(seu|sua)",

    # Discurso de ódio (raça, gênero, orientação)
    r"(?i)viad[oa]+",           # homofobia
    r"(?i)sapat[ãa]o",          # homofobia
    r"(?i)travec[oa]+",         # transfobia (pejorativo)
    r"(?i)macac[oa]+",          # racismo (contexto ofensivo)
    r"(?i)crioul[oa]+",         # racismo
    r"(?i)negr[oa]\s+imundo",   # racismo explícito

    # Assédio sexual explícito
    r"(?i)chupa\s+m(eu|inha)",
    r"(?i)vou\s+te\s+(comer|estuprar)",

    # Incitação a violência/autolesão
    r"(?i)se\s+mata",
    r"(?i)vai\s+se\s+matar",
]

# Pre-compiled toxic patterns (compiled once at import time)
_COMPILED_TOXIC_PATTERNS = [re.compile(p) for p in TOXIC_BLOCK_PATTERNS]


def security_enabled(agent_config: Dict[str, Any]) -> bool:
    """Resolução única do flag `security_settings.enabled` (default False).

    Fonte de verdade compartilhada entre `SmithGuardrail` e o orchestrator (que
    a usa para o ContextVar `prompt_safety_enabled`) — garante que as duas
    camadas NUNCA divirjam.
    """
    return bool(agent_config.get("security_settings", {}).get("enabled", False))


class SmithGuardrail:
    """
    Orquestrador de Segurança para Agentes.

    Pipeline de validação:
    1. Check Secret Keys (Pre-check)
    2. Prompt Injection Patterns (Pre-check)
    2.5. Toxicity Patterns (Regex local PT-BR)
    3. Custom Regex (Pre-check)
    4. PII Detection (Presidio)
    5. AI Safety — Prompt Guard 2 (Jailbreak via Groq)
    6. URL Whitelist

    NOTA (Mar/2026): Llama Guard 4 foi descontinuado pelo Groq.
    O check de toxicidade via modelo foi removido. A proteção agora
    depende dos TOXIC_BLOCK_PATTERNS (regex locais) no step 2.5.

    Returns:
        Tuple[bool, str, str]: (is_blocked, reason, sanitized_text)
    """

    def __init__(self, agent_config: Dict[str, Any], company_id: str):
        self.config = agent_config.get("security_settings", {})
        self.enabled = security_enabled(agent_config)
        self.fail_close = self.config.get("fail_close", True)
        self.company_id = company_id

        # Services (Singleton)
        self.presidio = get_presidio_service()
        self.safety_service = get_llama_guard_service()

        # Use pre-compiled module-level patterns (compiled once at import)
        self._compiled_injection_patterns = _COMPILED_INJECTION_PATTERNS
        self._compiled_toxic_patterns = _COMPILED_TOXIC_PATTERNS

    def _resolve_pii_config(self) -> Tuple[List[str] | None, float | None]:
        """Resolve a config de PII por-empresa para o Presidio (P1).

        Lê `security_settings.pii_entities` (allowlist opcional) e
        `security_settings.pii_score_threshold` (float opcional). Tolera valores
        malformados retornando `None` no campo correspondente — assim o
        `PresidioService` cai no DEFAULT seguro (sem PERSON, sem topônimos).

        Retorna `(entities, score_threshold)`; ambos podem ser `None`.
        """
        # entities: aceita lista de strings; qualquer outra coisa → None.
        entities_raw = self.config.get("pii_entities")
        entities: List[str] | None
        if isinstance(entities_raw, (list, tuple)) and all(
            isinstance(e, str) for e in entities_raw
        ):
            cleaned = [e for e in entities_raw if e.strip()]
            entities = cleaned or None
        else:
            entities = None

        # score_threshold: aceita número 0..1; qualquer outra coisa → None.
        threshold_raw = self.config.get("pii_score_threshold")
        score_threshold: float | None
        try:
            if threshold_raw is None or isinstance(threshold_raw, bool):
                score_threshold = None
            else:
                value = float(threshold_raw)
                score_threshold = value if 0.0 <= value <= 1.0 else None
        except (TypeError, ValueError):
            score_threshold = None

        return entities, score_threshold

    def _check_prompt_injection(self, text: str) -> Tuple[bool, str]:
        """Verifica padrões conhecidos de prompt injection (Rápido)."""
        for pattern in self._compiled_injection_patterns:
            if pattern.search(text):
                return True, "prompt_injection_pattern"
        return False, ""

    def _check_toxicity_patterns(self, text: str) -> Tuple[bool, str]:
        """Verifica padrões conhecidos de toxicidade grave (Rápido)."""
        for pattern in self._compiled_toxic_patterns:
            if pattern.search(text):
                return True, "toxic_content_pattern"
        return False, ""

    def _safe_regex_search(self, pattern: str, text: str) -> Tuple[bool, str]:
        """Executa regex com timeout para prevenir ReDoS."""
        def _search():
            try:
                compiled = re.compile(pattern, re.IGNORECASE)
                match = compiled.search(text)
                if match:
                    return True, match.group()[:20]
                return False, ""
            except re.error:
                return False, ""

        future = _REGEX_EXECUTOR.submit(_search)
        try:
            return future.result(timeout=REGEX_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            future.cancel()
            logger.error("[GUARDRAIL] ⏱️ Regex TIMEOUT (ReDoS?)")
            return False, ""

    async def validate_input(self, text: str) -> Tuple[bool, str, str]:
        """Executa pipeline de validação.

        Pipeline em duas camadas:

        1. BASELINE OBRIGATÓRIO (roda SEMPRE, antes do gate `enabled`, a menos
           que `GUARDRAIL_BASELINE_ENABLED=False` desligue globalmente):
           prompt-injection regex + toxicidade regex (pré-compilados, custo
           desprezível, SEM chamada de rede).
        2. OPT-IN POR-AGENTE (só com `security_settings.enabled == True`):
           - Prompt Guard 2 (Groq, anti-jailbreak): só com
             `security_settings.check_jailbreak == True`. É a ÚNICA camada com
             custo/latência de rede (1 round-trip Groq por mensagem, em série
             antes do 1º token); por isso fica atrás do opt-in por-agente, e não
             no baseline.
           - secret-keys, custom_regex, PII (Presidio, offloadado) e URL whitelist.
        """
        user_error_message = self.config.get(
            "error_message",
            "Sua mensagem viola as políticas de segurança."
        )
        sanitized_text = text

        # ═══════════════════════════════════════════════════════════════
        # GATE 100% POR-AGENTE: sem `security_settings.enabled == True`, NADA
        # roda — zero regex, zero Groq, zero PII. Quem decide é o agente do
        # cliente. Sem opt-in → passthrough total (o Smith atende cliente real;
        # guardrail genérico-global só gera falso-positivo e quebra a aplicação).
        # ═══════════════════════════════════════════════════════════════
        if not self.enabled:
            return False, "", sanitized_text

        # Prompt injection (regex PT-BR/EN, pré-compilado).
        is_injection, _ = self._check_prompt_injection(text)
        if is_injection:
            logger.warning(f"[GUARDRAIL] 🚫 Prompt injection pattern detected for company {self.company_id}")
            return True, user_error_message, text

        # Toxicidade grave (regex PT-BR, pré-compilado).
        is_toxic, _ = self._check_toxicity_patterns(text)
        if is_toxic:
            logger.warning(f"[GUARDRAIL] 🚫 Toxic pattern detected for company {self.company_id}")
            return True, user_error_message, text

        # AI Safety — Prompt Guard 2 (jailbreak via Groq), só com
        # `security_settings.check_jailbreak`. FAIL-OPEN: erro de infra (Groq
        # fora/timeout) NUNCA bloqueia — não pode derrubar cliente real.
        if self.config.get("check_jailbreak", True):
            try:
                is_unsafe, _ = await self.safety_service.validate_all(
                    text,
                    check_jailbreak=True,
                    check_nsfw=False,
                    fail_close=False,
                )
            except Exception:  # noqa: BLE001 — fail-open
                is_unsafe = False
            if is_unsafe:
                return True, user_error_message, text

        # ═══════════════════════════════════════════════════════════════
        # 1. CHECK SECRET KEYS (Regex rápido, síncrono)
        # ═══════════════════════════════════════════════════════════════
        if self.config.get("check_secret_keys", True):
            if self._has_secret_keys(text):
                logger.warning(f"[GUARDRAIL] 🔑 Secret key detected for company {self.company_id}")
                return True, user_error_message, text

        # ═══════════════════════════════════════════════════════════════
        # 2. CUSTOM REGEX (Com proteção ReDoS)
        # ═══════════════════════════════════════════════════════════════
        custom_regexes = self.config.get("custom_regex", [])
        for pattern in custom_regexes:
            try:
                matched, _ = self._safe_regex_search(pattern, text)
                if matched:
                    logger.warning(f"[GUARDRAIL] 🔍 Custom regex matched for company {self.company_id}")
                    return True, user_error_message, text
            except Exception as e:
                logger.error(f"[GUARDRAIL] ❌ Regex error: {e}")
                continue

        # ═══════════════════════════════════════════════════════════════
        # 3. PII DETECTION (Presidio) — offloadado p/ thread (F22), CPU-bound
        #    spaCy não pode serializar o event loop. Input truncado antes do
        #    parse (bound de DoS).
        # ═══════════════════════════════════════════════════════════════
        pii_action = self.config.get("pii_action", "mask")
        if pii_action != "off":
            bounded_text = text[:PRESIDIO_MAX_INPUT_CHARS]
            pii_entities, pii_threshold = self._resolve_pii_config()
            found_pii, processed_text = await asyncio.to_thread(
                self.presidio.analyze_and_anonymize,
                bounded_text,
                action=pii_action,
                entities=pii_entities,
                score_threshold=pii_threshold,
            )

            if found_pii:
                if pii_action == "block":
                    logger.warning(f"[GUARDRAIL] 🛡️ PII blocked for company {self.company_id}")
                    return True, f"{user_error_message} (Dados pessoais detectados)", text
                else:
                    sanitized_text = processed_text
                    logger.info("[GUARDRAIL] 🎭 PII masked - using sanitized text")

        # ═══════════════════════════════════════════════════════════════
        # 4. URL WHITELIST
        # ═══════════════════════════════════════════════════════════════
        if self.config.get("check_urls", False):
            is_valid, invalid_url = self._validate_urls(sanitized_text)
            if not is_valid:
                logger.warning(f"[GUARDRAIL] 🔗 URL blocked: {invalid_url}")
                return True, f"{user_error_message} (URL não permitida)", sanitized_text

        # ═══════════════════════════════════════════════════════════════
        # PASSOU EM TUDO
        # ═══════════════════════════════════════════════════════════════
        logger.debug("[GUARDRAIL] ✅ Message passed all checks")
        return False, "", sanitized_text

    async def validate_output(self, text: str) -> Tuple[bool, str, str]:
        """Moderação de SAÍDA da resposta do LLM antes do egress (F21).

        Estágio simétrico a `validate_input` `(is_blocked, reason, sanitized_text)`,
        aplicado sobre a resposta FINAL do assistente no chokepoint agregado
        (`run_turn` → cobre widget + WhatsApp) e sobre o `full_response` acumulado
        do streaming ANTES da persistência. Reaproveita os helpers já existentes —
        NÃO há nó novo no grafo, NÃO chama Prompt Guard de novo (o input já foi
        checado; foco em custo): **toxicidade + PII mask + URL whitelist**.

        Camadas (mesma postura do `validate_input`):

        1. BASELINE (sob `GUARDRAIL_BASELINE_ENABLED`, mesmo kill-switch do F20):
           toxicidade regex (`_check_toxicity_patterns`, pré-compilada). Em
           toxicidade → devolve a CÓPIA SEGURA (não vaza o texto tóxico ao
           usuário/DB), simétrico ao bloqueio de entrada.
        2. OPT-IN POR-TENANT (lido de `self.config`, idêntico ao input):
           - PII `mask` via `presidio.analyze_and_anonymize(action="mask")`,
             offloadado p/ thread (CPU-bound spaCy não pode serializar o loop) e
             truncado a `PRESIDIO_MAX_INPUT_CHARS` (bound de DoS). Gateado por
             `pii_action != "off"` do tenant; na saída SEMPRE mascara (nunca
             "block": o egress sanitiza, não rejeita a resposta inteira).
           - URL whitelist/blacklist via `_validate_urls` (só roda quando
             `url_protection_mode` está ligado no tenant — caça links de
             phishing/exfiltração que o MODELO emite). URL fora da política →
             cópia segura.

        ASSIMETRIA do streaming (G6/F23): no modo streaming os tokens são
        entregues AO VIVO crus (sem buffering — bufferizar anularia o streaming);
        este estágio sana apenas o texto PERSISTIDO/relido em histórico. Bloqueio
        hard token-a-token fica fora de escopo (precisaria de buffering).
        """
        sanitized_text = text
        if not text:
            return False, "", sanitized_text

        safe_message = self.config.get(
            "output_error_message",
            "Desculpe, não posso fornecer essa resposta.",
        )

        # ═══════════════════════════════════════════════════════════════
        # GATE 100% POR-AGENTE: sem `security_settings.enabled == True`, NADA
        # modera na saída (nem toxicidade, nem PII). Quem decide é o agente.
        # ═══════════════════════════════════════════════════════════════
        if not self.enabled:
            return False, "", sanitized_text

        # Toxicidade na SAÍDA (regex). Devolve a cópia segura (não entrega tóxico).
        is_toxic, _ = self._check_toxicity_patterns(text)
        if is_toxic:
            logger.warning(
                f"[GUARDRAIL] 🚫 Toxic OUTPUT blocked for company {self.company_id}"
            )
            return True, safe_message, safe_message

        # PII mask (Presidio) — offloadado p/ thread + input truncado (F22).
        # Na saída SEMPRE mascara (action="mask"); "block" não se aplica ao egress.
        pii_action = self.config.get("pii_action", "mask")
        if pii_action != "off":
            bounded_text = sanitized_text[:PRESIDIO_MAX_INPUT_CHARS]
            pii_entities, pii_threshold = self._resolve_pii_config()
            found_pii, processed_text = await asyncio.to_thread(
                self.presidio.analyze_and_anonymize,
                bounded_text,
                action="mask",
                entities=pii_entities,
                score_threshold=pii_threshold,
            )
            if found_pii:
                # Reanexa qualquer cauda além do teto (não analisada) ao texto
                # mascarado, para não truncar a resposta entregue ao usuário.
                sanitized_text = processed_text + sanitized_text[PRESIDIO_MAX_INPUT_CHARS:]
                logger.info("[GUARDRAIL] 🎭 PII masked on OUTPUT - using sanitized text")

        # URL whitelist/blacklist (só ativo quando o tenant liga
        # `url_protection_mode`) — bloqueia phishing/exfil que o modelo emite.
        if self.config.get("check_urls", False):
            is_valid, invalid_url = self._validate_urls(sanitized_text)
            if not is_valid:
                logger.warning(f"[GUARDRAIL] 🔗 URL blocked on OUTPUT: {invalid_url}")
                return True, safe_message, safe_message

        return False, "", sanitized_text

    def _has_secret_keys(self, text: str) -> bool:
        """Detecta padrões comuns de API keys."""
        patterns = [
            r"sk-[a-zA-Z0-9]{32,}",           # OpenAI legacy
            r"sk-proj-[a-zA-Z0-9-]{32,}",     # OpenAI Project
            r"sk-ant-[a-zA-Z0-9-]{32,}",      # Anthropic
            r"ghp_[a-zA-Z0-9]{36}",           # GitHub PAT
            r"github_pat_[a-zA-Z0-9]{22}_[a-zA-Z0-9]{59}",  # GitHub fine-grained
            r"xox[baprs]-[a-zA-Z0-9]{10,}",   # Slack
            r"AIza[0-9A-Za-z-_]{35}",         # Google API
            r"gsk_[a-zA-Z0-9]{52}",           # Groq
            r"AKIA[0-9A-Z]{16}",              # AWS Access Key
            r"eyJ[a-zA-Z0-9_-]*\.eyJ[a-zA-Z0-9_-]*\.[a-zA-Z0-9_-]*",  # JWT
        ]
        for p in patterns:
            if re.search(p, text):
                return True
        return False

    def _validate_urls(self, text: str) -> Tuple[bool, str]:
        """
        Valida URLs contra whitelist ou blacklist, dependendo do modo.
        """
        mode = self.config.get("url_protection_mode", "off")

        if mode == "off":
            return True, ""

        # URLs com protocolo (sempre confiável)
        url_pattern_with_protocol = r"https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+"
        urls_with_protocol = re.findall(url_pattern_with_protocol, text)

        # URLs sem protocolo (limitado a TLDs conhecidos)
        # Regex: dominio.tld ou dominio.tld/path
        # (?<!@) evita pegar emails
        url_pattern_noprotocol = rf"(?<![@\w])(?:[a-zA-Z0-9][-a-zA-Z0-9]*\.)+{_TLD_PATTERN}(?:/[^\s]*)?"
        urls_without_protocol = re.findall(url_pattern_noprotocol, text, re.IGNORECASE)

        # Combina e remove duplicatas
        all_urls = list(set(urls_with_protocol + urls_without_protocol))

        if not all_urls:
            return True, ""

        # Normaliza todos os domínios
        normalized_urls = [self._normalize_domain(u) for u in all_urls]

        if mode == "whitelist":
            whitelist = self.config.get("url_whitelist", [])
            if not whitelist:
                return False, all_urls[0]

            for i, domain in enumerate(normalized_urls):
                if not self._is_in_list(domain, whitelist):
                    return False, all_urls[i]

        elif mode == "blacklist":
            blacklist = self.config.get("url_blacklist", DEFAULT_BLACKLIST if self.config.get("url_blacklist") is None else self.config.get("url_blacklist"))

            for i, domain in enumerate(normalized_urls):
                if self._is_in_list(domain, blacklist):
                    return False, all_urls[i]

        return True, ""

    def _normalize_domain(self, url_or_domain: str) -> str:
        """Remove protocolo, trailing slash e www."""
        d = url_or_domain.lower().strip()
        if d.startswith("http://"):
            d = d[7:]
        elif d.startswith("https://"):
            d = d[8:]
        if d.startswith("www."):
            d = d[4:]
        return d.split("/")[0] # Retorna apenas domínio base

    def _is_in_list(self, domain: str, pattern_list: List[str]) -> bool:
        """Verifica match considerando wildcards e subdomínios."""
        for pattern in pattern_list:
            clean_pattern = self._normalize_domain(pattern)

            # Exact match
            if domain == clean_pattern:
                return True

            # Subdomain match (ex: blog.site.com entra em site.com)
            if domain.endswith("." + clean_pattern):
                return True

            # Wildcard explicit (ex: *.site.com)
            if clean_pattern.startswith("*.") and domain.endswith(clean_pattern[1:]):
                return True

        return False
